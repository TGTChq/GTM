"""Cross-run dedup / suppression state, persisted atomically under
``/app/data/state/orchestrator_v2/seen_suppression``.

Only two kinds of key are persisted, and both suppress a *specific artifact*, not
a company:

* ``postings``  -- exact posting-identity keys already processed to completion.
  A repeat of the *same* posting is skipped before acquisition/enrichment; a
  NEW posting (new identity) from the same company is untouched, so a fresh
  hiring signal is never permanently blocked.
* ``delivered`` -- Airtable ``lead_key``s already created/existing. A repeat of
  the *same* contact/opportunity is not re-delivered. This is a local mirror of
  Airtable's own server-side ``lead_key`` idempotency, not a replacement for it.

There is deliberately **no** company-wide exclusion set here: the repository's
existing company-suppression semantics (CRM/Airtable/campaign rules) stay where
they are and are not duplicated or overridden.

Writes are atomic (temp + os.replace via ``StateManager.write_json``), schema
versioned, and corruption-safe on load.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Set

from retrieval_measurement.identity import utc_stamp

SUPPRESSION_SCHEMA = "orchestrator-suppression/1"


class SuppressionStore:
    POSTINGS = "postings.json"
    DELIVERED = "delivered_leads.json"

    def __init__(self, state: Any) -> None:
        self.state = state

    def _load(self, name: str) -> Set[str]:
        try:
            data = self.state.read_json("seen_suppression", name, require_schema=False)
        except Exception:  # noqa: BLE001 - a corrupt file is treated as empty, not fatal
            data = None
        if not isinstance(data, dict):
            return set()
        return {str(k) for k in (data.get("keys") or [])}

    def _save(self, name: str, keys: Set[str]) -> None:
        self.state.write_json("seen_suppression", name, {
            "schema_version": SUPPRESSION_SCHEMA,
            "count": len(keys),
            "updated_at": utc_stamp(),
            "keys": sorted(keys),
        })

    # -- posting-identity dedup -------------------------------------------

    def seen_postings(self) -> Set[str]:
        return self._load(self.POSTINGS)

    def commit_postings(self, keys: Iterable[str]) -> Set[str]:
        merged = self.seen_postings() | {str(k) for k in keys if k}
        self._save(self.POSTINGS, merged)
        return merged

    # -- delivered lead_key dedup -----------------------------------------

    def delivered_leads(self) -> Set[str]:
        return self._load(self.DELIVERED)

    def commit_delivered(self, keys: Iterable[str]) -> Set[str]:
        merged = self.delivered_leads() | {str(k) for k in keys if k}
        self._save(self.DELIVERED, merged)
        return merged

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seen_postings": len(self.seen_postings()),
            "delivered_leads": len(self.delivered_leads()),
            "schema_version": SUPPRESSION_SCHEMA,
        }
