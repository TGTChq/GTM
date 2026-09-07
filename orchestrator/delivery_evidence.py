"""Compact original writer receipts; never infer approval from a creation count."""
from __future__ import annotations


def delivery_evidence(delivery: dict) -> dict:
    detail = delivery.get("detail") or {}
    airtable = detail.get("airtable") or {}
    keys = airtable.get("created_approved_lead_keys")
    reasons = airtable.get("not_written_send_safe_reasons")
    has_reasons = isinstance(reasons, dict)
    withheld = delivery.get("send_safe_withheld")
    if withheld is None:
        withheld = (delivery.get("skip_breakdown") or {}).get("send_safe_withheld")
    return {
        "basis": "original_airtable_writer_receipts",
        "created": delivery.get("created"),
        "confirmed_approved": len(set(keys)) if isinstance(keys, list) else None,
        "created_approval_status_unknown": airtable.get("created_approval_status_unknown"),
        "approval_keys_recorded": isinstance(keys, list),
        "send_safe_withheld": withheld,
        "send_safe_reasons": dict(reasons) if has_reasons else None,
        "send_safe_reasons_reconcile": (
            sum(reasons.values()) == withheld
            if has_reasons and withheld is not None else None),
        # A recomputation of enriched files has different population and signing
        # inputs. It cannot be substituted for the original writer's reason map.
        "per_contact_original_reasons_available": False,
    }
