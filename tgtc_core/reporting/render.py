"""The readable report.

What a reader gets is counts, definitions and named reasons -- never a person. No
name, email, phone number or company-contact row appears in this text, so the summary
can be pasted into a channel or forwarded without exposing anybody's personal data.
The lead-level detail lives in the separate export (``reporting.export``), which
writes outside the repository and is shared deliberately rather than by default.
``tests_core/test_weekly_report.py`` asserts that property on a report built from
rows that DO contain personal data.
"""

from __future__ import annotations

from typing import Any, Dict, List

WIDTH = 78


def _n(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _reasons(mapping: Dict[str, int], limit: int = 6) -> str:
    if not mapping:
        return "none"
    items = sorted(mapping.items(), key=lambda kv: -kv[1])[:limit]
    return ", ".join(f"{k} {v:,}" for k, v in items)


def _rule(title: str) -> List[str]:
    return ["", title, "-" * min(WIDTH, max(len(title), 12))]


def render_text(report: Dict[str, Any]) -> str:
    w, d, j = report["window"], report["delivery"], report["jobs"]
    a, c, u, b = report["acquisition"], report["contacts"], report["units"], report["backlog"]
    runs, recon, spend = report["runs"], report["reconciliation"], report["spend"]
    kind = "WEEK" if w["kind"] == "weekly" else "WEEK TO DATE (partial -- not a closed week)"
    out: List[str] = [
        "TGTC hiring-intent pipeline -- weekly report",
        f"{kind}: {w['window_label']}  ({w['iso_week']})",
        f"Window: {w['window_start_local']} to {w['window_end_local']} {w['timezone']}, {w['interval']}",
        f"Generated {report['generated_at']} | data cutoff {w['data_cutoff_utc']} | "
        f"source: {report['source']['database']}",
        f"Status: {report['status'].upper()}" + (f" ({len(report.get('alerts', []))} alert(s))"
                                                 if report.get("alerts") else ""),
    ]

    out += _rule("THE BUSINESS TARGET -- net-new leads created in the nine Challenger campaigns")
    out += [
        f"  Genuine Instantly creations (unique people) : {_n(d['instantly_created_unique_people'])}",
        f"    from approvals made this week             : {_n(b['created_from_this_weeks_approvals'])}",
        f"    from earlier approvals (backlog)          : {_n(b['created_from_earlier_approvals_backlog'])}",
        f"  Production runs in the window               : {_n(runs['run_count'])} "
        f"({_n(runs['completed_runs'])} completed, {_n(runs['refused_runs'])} refused)",
    ]
    if runs["local_days_without_a_run"]:
        out.append(f"  Days with NO run (missing, not zero)        : {', '.join(runs['local_days_without_a_run'])}")

    out += _rule("FUNNEL -- each stage in its own unit; nothing here is subtracted from anything")
    out += [
        f"  Fantastic records returned                  : {_n(a['fantastic_records_returned'])} "
        f"(unique ids {_n(a['fantastic_records_unique_ids'])}, requests {_n(a['fantastic_requests'])})",
        f"  Fantastic records billed                    : confirmed {_n(a['fantastic_records_billed_confirmed'])} "
        f"| estimate {_n(a['fantastic_records_billed_estimate'])}",
        f"  New unique jobs                             : {_n(j['new_jobs_unique'])} "
        f"(duplicates of existing {_n(j['new_jobs_duplicates_of_existing'])})",
        f"  Jobs reviewed                               : {_n(j['jobs_reviewed'])}",
        f"    qualified {_n(j['jobs_qualified'])} | rejected {_n(j['jobs_rejected_excluded'])} "
        f"| no campaign fit {_n(j['jobs_no_campaign_fit'])} | pending review {_n(j['jobs_pending_review'])}",
        f"    top rejection reasons: {_reasons(j['rejection_reasons'])}",
        f"  New employers                               : {_n(u['new_employers'])}",
        f"  New company x campaign units                : {_n(u['new_units'])} "
        f"(worked for contacts {_n(u['units_worked_for_contacts'])})",
        f"  Contacts found                              : {_n(c['candidates_found'])} "
        f"(enriched {_n(c['contacts_enriched'])})",
        f"  Emails verified                             : {_n(c['emails_verified'])}",
        f"  Contacts approved                           : {_n(c['contacts_approved'])}",
        f"    compliance-blocked (counted capacity, never sendable): {_n(c['compliance_blocked'])} "
        f"[{_reasons(c['compliance_blocked_by_reason'], 3)}]",
        f"    held by a contact gate: {_reasons(c['contacts_rejected_by_gate'], 4)}",
        f"    suppressions added: {_reasons(c['suppressions_added'], 3)}",
        f"  Instantly: created {_n(d['instantly_created_unique_people'])} | "
        f"already existing {_n(d['instantly_already_existing'])} | rejected {_n(d['instantly_rejected'])} "
        f"[{_reasons(d['instantly_rejected_by_reason'], 3)}]",
        f"  Airtable records created                    : {_n(d['airtable_records_created'])} "
        f"(recovered by reconciliation {_n(d['airtable_records_recovered_by_reconciliation'])})",
        f"  Delivery blocked                            : {_reasons(d['delivery_blocked_by_reason'], 5)}",
        f"  Delivery failed                             : {_reasons(d['delivery_failed_by_channel'], 3)}",
        f"  Approved this week, no Instantly answer yet : {_n(b['approved_this_week_not_yet_delivered'])}",
        f"  Approved this week, held with a named reason: {_n(b['approved_this_week_held_with_a_named_reason'])}",
    ]

    out += _rule("BY CAMPAIGN (all nine)")
    out.append(f"  {'campaign':<22}{'units':>7}{'approved':>10}{'created':>9}{'airtable':>10}{'blocked':>9}")
    for key, row in report["by_campaign"]["campaigns"].items():
        out.append(f"  {row['name'][:22]:<22}{row['new_units']:>7,}{row['contacts_approved']:>10,}"
                   f"{row['instantly_created_unique_people']:>9,}{row['airtable_records_created']:>10,}"
                   f"{row['compliance_blocked']:>9,}")
    if report["by_campaign"]["campaign_keys_outside_the_nine"]:
        out.append(f"  OUTSIDE THE NINE: {', '.join(report['by_campaign']['campaign_keys_outside_the_nine'])}")

    out += _rule("DAILY (local days in the report's own timezone)")
    for day in report["daily"]:
        out.append(f"  {day['weekday']} {day['date']}  created {day['instantly_created_unique_people']:>6,} | "
                   f"approved {day['contacts_approved']:>6,} | jobs {day['new_jobs']:>7,} | "
                   f"Apollo {_n(day['apollo_credits']):>8} cr | Fantastic {_n(day['fantastic_records']):>8}")

    if report.get("previous_week"):
        out += _rule(f"AGAINST THE PREVIOUS WEEK ({report['previous_week']['window']})")
        for metric, row in report["previous_week"]["metrics"].items():
            change = row["change"]
            arrow = "" if change is None else (f"  {change:+,}")
            out.append(f"  {metric:<38}{_n(row['this_week']):>10}  (was {_n(row['previous_week'])}){arrow}")

    out += _rule("RECONCILIATION -- Airtable records = genuine creations + records without one")
    out += [
        f"  Airtable records created                    : {_n(recon['airtable_records_created'])}",
        f"  Genuine Instantly creations (approvals)     : {_n(recon['genuine_instantly_creations_approvals'])}",
        f"  Written without a genuine creation          : {_n(recon['airtable_records_without_a_genuine_creation'])}"
        f" [{_reasons(recon['airtable_records_without_a_creation_by_reason'], 4)}]",
        f"  Unexplained difference                      : {_n(recon['unexplained_difference'])} "
        f"(identity holds: {_n(recon['identity_holds'])})",
    ]
    for run in recon["per_run"]:
        out.append(f"    run {run['run_id']}: Instantly {run['instantly_created']:,} | "
                   f"Airtable {run['airtable_records']:,} | difference {run['difference']:+,}")

    out += _rule("SPEND")
    for provider, row in spend["providers"].items():
        out.append(f"  {provider:<12} requests {_n(row['requests']):>8} | credits "
                   f"{_n(row['credits_confirmed'] if row['credits_confirmed'] is not None else row['credits_estimated']):>10}"
                   f" | per final lead {_n(row['credits_per_final_lead']):>8} | "
                   f"{'$' + _n(row['spend_usd']) if row['spend_usd'] is not None else row['unit_price_basis']}")
    out.append(f"  Cost per final lead (USD)                   : {_n(spend['cost_per_final_lead_usd'])}")
    out.append(f"  ({spend['note']})")

    cum = report["cumulative"]
    out += _rule(f"CUMULATIVE SINCE {cum['since']} -- a DIFFERENT view: how much exists, not this week")
    out += [
        f"  Jobs seen {_n(cum['jobs_seen'])} | employers {_n(cum['employers'])} | units {_n(cum['units'])}",
        f"  Contacts approved {_n(cum['contacts_approved'])} | "
        f"genuine Instantly creations {_n(cum['instantly_created_unique_people'])} | "
        f"Airtable records {_n(cum['airtable_records'])}",
        f"  Apollo credits {_n(cum['apollo_credits'])} | Fantastic records {_n(cum['fantastic_records'])}",
    ]

    legacy = report["legacy_airtable_review"]
    out += _rule("LEGACY AIRTABLE REVIEW POPULATION -- not delivered leads")
    out += [
        f"  Airtable records with no genuine Instantly creation behind them: {_n(legacy['records'])}",
        f"    {_reasons(legacy['by_reason'], 6)}",
        f"  {legacy['handling']}",
    ]

    out += _rule("ALERTS -- production or data integrity needs a person")
    out += [f"  ! {flag}" for flag in report.get("alerts", [])] or ["  none: every check in this report passed"]
    notes = report.get("notes", [])
    if notes:
        out += _rule("NOTES ON THIS REPORT")
        out += [f"  - {note}" for note in notes]

    out += _rule("NOTES")
    out += [
        f"  - {report['source']['sidecar_note']}.",
        "  - Net-new lead = a person Instantly answered 'created' for, in the campaign the approval was "
        "routed to. Existing, rejected, Control and repeat people are excluded.",
        "  - Records, jobs, units, contacts, Airtable records and Instantly creations are different units "
        "and are never added together.",
        "  - No personal data appears in this summary; the lead-level detail is a separate, private export.",
    ]
    return "\n".join(out) + "\n"


def render_slack(report: Dict[str, Any], *, limit: int = 3800) -> str:
    """The same report as a Slack message: a monospaced block, truncated at a line
    boundary with an explicit marker rather than silently cut mid-number."""
    text = render_text(report)
    head = f"*TGTC weekly report -- {report['window']['window_label']}* ({report['status'].upper()})"
    budget = limit - len(head) - 20
    if len(text) > budget:
        kept: List[str] = []
        used = 0
        for line in text.splitlines():
            if used + len(line) + 1 > budget:
                break
            kept.append(line)
            used += len(line) + 1
        text = "\n".join(kept) + "\n[truncated -- the full report is the stored artifact]"
    return f"{head}\n```\n{text}\n```"
