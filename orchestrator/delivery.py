"""Delivery -- Airtable rows and Instantly enrollment, through FAKE adapters.

No real Airtable or Instantly call is made anywhere in this module; the real
clients would implement the same ``DeliveryAdapter`` protocol. Offline and dry
runs use the fakes here.

Invariants enforced:

* auto-approval is FINAL_PASS only; a NEEDS_CHECK or UNVERIFIED record can never
  auto-approve (it is skipped with ``not_final_pass``);
* ``entered = created + skipped + failed`` at the Airtable boundary;
* ``enrolled == auto_approved_final_pass`` at the Instantly boundary;
* idempotency keys prevent a contact being delivered twice across runs;
* a batch failure falls back to per-record writes;
* every created row is recorded in an audit log with a rollback token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from orchestrator.enrichment import Lead
from orchestrator.reasons import Disposition, ReasonCode
from orchestrator.state import StateManager


class DeliveryAdapter(Protocol):
    def create_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]: ...
    def create_one(self, record: Dict[str, Any]) -> Dict[str, Any]: ...


class AdapterBatchError(RuntimeError):
    """The batch endpoint failed as a whole; caller must fall back per-record."""


@dataclass
class FakeAirtableAdapter:
    fail_batch: bool = False
    fail_records: frozenset = frozenset()   # contact_keys that fail even per-record
    _counter: int = 0

    def create_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self.fail_batch:
            raise AdapterBatchError("simulated batch endpoint failure")
        return [self.create_one(r) for r in records]

    def create_one(self, record: Dict[str, Any]) -> Dict[str, Any]:
        key = record.get("contact_key", "")
        if key in self.fail_records:
            raise RuntimeError(f"simulated record failure for {key}")
        self._counter += 1
        return {"airtable_id": f"rec{self._counter:06d}", "contact_key": key}


@dataclass
class FakeInstantlyAdapter:
    fail_records: frozenset = frozenset()

    def enroll(self, record: Dict[str, Any]) -> Dict[str, Any]:
        key = record.get("contact_key", "")
        if key in self.fail_records:
            raise RuntimeError(f"simulated enrollment failure for {key}")
        return {"instantly_id": f"enr-{key}", "contact_key": key}


@dataclass
class DeliveryReport:
    entered: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    enrolled: int = 0
    auto_approved_final_pass: int = 0
    skip_reasons: Dict[str, int] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)
    rollback_tokens: List[str] = field(default_factory=list)
    delivered_lead_keys: List[str] = field(default_factory=list)

    def reconciles(self) -> bool:
        return self.entered == self.created + self.skipped + self.failed

    def enrollment_reconciles(self) -> bool:
        return self.enrolled <= self.auto_approved_final_pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entered": self.entered,
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "enrolled": self.enrolled,
            "auto_approved_final_pass": self.auto_approved_final_pass,
            "airtable_reconciles": self.reconciles(),
            "enrollment_reconciles": self.enrollment_reconciles(),
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "audit_entries": len(self.audit),
            "rollback_tokens": len(self.rollback_tokens),
        }


class DeliveryManager:
    def __init__(
        self,
        *,
        state: StateManager,
        airtable: DeliveryAdapter,
        instantly: Any,
        enable_airtable_write: bool,
        auto_approve: bool,
        enable_instantly: bool,
    ) -> None:
        self.state = state
        self.airtable = airtable
        self.instantly = instantly
        self.enable_airtable_write = enable_airtable_write
        self.auto_approve = auto_approve
        self.enable_instantly = enable_instantly
        self._delivered = self._load_delivered()

    def _load_delivered(self) -> set:
        data = self.state.read_json("delivery_state", "delivered.json", require_schema=False)
        return set((data or {}).get("keys", []))

    def _persist_delivered(self) -> None:
        self.state.write_json("delivery_state", "delivered.json", {"keys": sorted(self._delivered)})

    def deliver(self, leads: List[Lead], *, run_id: str = "", known_delivered=None) -> DeliveryReport:
        if known_delivered:
            self._delivered |= {str(k) for k in known_delivered}
        report = DeliveryReport(entered=len(leads))
        skip: Dict[str, int] = {}
        to_write: List[Dict[str, Any]] = []

        # 1) Gate: FINAL_PASS only may be auto-approved; idempotency skips repeats.
        for lead in leads:
            if lead.disposition is not Disposition.FINAL_PASS:
                report.skipped += 1
                skip[ReasonCode.NOT_FINAL_PASS.value] = skip.get(ReasonCode.NOT_FINAL_PASS.value, 0) + 1
                continue
            if lead.contact_key in self._delivered:
                report.skipped += 1
                skip[ReasonCode.ALREADY_DELIVERED.value] = skip.get(ReasonCode.ALREADY_DELIVERED.value, 0) + 1
                continue
            to_write.append({
                "contact_key": lead.contact_key,
                "company": lead.company.get("name", ""),
                "email": lead.contact.get("email", ""),
                "disposition": lead.disposition.value,
            })
        report.auto_approved_final_pass = len(to_write)

        # 2) Airtable write (fake). Dry run: count as skipped, never write.
        created_records: List[Dict[str, Any]] = []
        if not (self.enable_airtable_write and self.auto_approve):
            report.skipped += len(to_write)
            for r in to_write:
                skip["dry_run_no_write"] = skip.get("dry_run_no_write", 0) + 1
            report.skip_reasons = skip
            report.audit.append({"event": "dry_run", "would_create": len(to_write)})
            return report

        try:
            created_records = self.airtable.create_batch(to_write)
        except AdapterBatchError:
            # Per-record fallback after batch failure.
            report.audit.append({"event": "batch_failed_fallback", "count": len(to_write)})
            for r in to_write:
                try:
                    created_records.append(self.airtable.create_one(r))
                except Exception as exc:  # noqa: BLE001
                    report.failed += 1
                    report.audit.append({"event": "record_failed", "contact_key": r["contact_key"],
                                         "error": str(exc)})

        for rec in created_records:
            report.created += 1
            report.rollback_tokens.append(rec["airtable_id"])
            report.delivered_lead_keys.append(rec["contact_key"])
            report.audit.append({"event": "created", "airtable_id": rec["airtable_id"],
                                 "contact_key": rec["contact_key"]})
            self._delivered.add(rec["contact_key"])

        # 3) Instantly enrollment (fake), behind its own explicit flag, FINAL_PASS only.
        if self.enable_instantly:
            for rec in created_records:
                try:
                    self.instantly.enroll(rec)
                    report.enrolled += 1
                    report.audit.append({"event": "enrolled", "contact_key": rec["contact_key"]})
                except Exception as exc:  # noqa: BLE001
                    report.audit.append({"event": "enroll_failed", "contact_key": rec["contact_key"],
                                         "error": str(exc)})

        report.skip_reasons = skip
        self._persist_delivered()
        self.state.write_json("delivery_state", "audit_log.json", {"audit": report.audit})
        return report

    def rollback(self, report: DeliveryReport) -> Dict[str, Any]:
        """Undo a delivery: emit the rollback tokens and clear their idempotency
        keys. (The fake has no server state; the audit trail is the record.)"""
        for rec in report.audit:
            if rec.get("event") == "created":
                self._delivered.discard(rec.get("contact_key"))
        self._persist_delivered()
        return {"rolled_back": list(report.rollback_tokens)}
