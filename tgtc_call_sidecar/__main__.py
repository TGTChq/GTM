"""``python -m tgtc_call_sidecar`` -- manual only; nothing here is scheduled.

  pilot         build up to 100 EMAIL_FOLLOW_UP + 100 CALL_FIRST completed, callable records
  disposition   record one call outcome (sidecar state only)
  after-core    the only form in which the sidecar may ever follow the core: it never raises,
                always exits 0, and does nothing while the core's run lock is held

Every command refuses to act unless TGTC_CALL_LIST_SIDECAR=1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import PilotConfig, enabled

OFF_MESSAGE = "call-list sidecar is off (TGTC_CALL_LIST_SIDECAR != 1); nothing done"


def cmd_pilot(args) -> int:
    from .pilot import Pilot, load_core_snapshot, load_crm, load_instantly, instantly_history
    from .providers import AirtableReadOnly, ApolloPhones, InstantlyReadOnly
    from .report import non_pii_report, write_call_file, write_report
    from .selection import default_william_paths, william_exclusions
    from .store import SidecarStore

    cfg = PilotConfig(apollo_credit_cap=args.credit_cap, target_per_cohort=args.target, max_in_flight=args.in_flight)
    env = os.environ
    apollo = ApolloPhones(env["APOLLO_API_KEY"])
    instantly = InstantlyReadOnly(env["INSTANTLY_API_KEY"])
    airtable = AirtableReadOnly(env["AIRTABLE_TOKEN"], env["AIRTABLE_BASE_ID"], env["AIRTABLE_TABLE_NAME"]) \
        if env.get("AIRTABLE_TOKEN") else None
    store = SidecarStore(args.state)
    run_id = store.start_run()
    log = lambda msg: print(msg, flush=True)  # noqa: E731 -- counts and states only, never PII
    units, units_by_opp, approved, core = load_core_snapshot(args.inputs)
    log(f"core snapshot: {len(units)} units, {len(approved)} approved contacts, {len(core.apollo_ids)} core people")
    leads, replied, bounced, unsub = load_instantly(instantly)
    log(f"instantly (read-only): {len(leads)} Challenger leads, {len(replied)} replied, {len(bounced)} bounced, "
        f"{len(unsub)} unsubscribed")
    # Fail closed: the workspace search must find a lead we know exists before it may clear anyone.
    probe = next(iter(leads), "")
    if not probe or not instantly_history(instantly, email=probe, first="", last="", domain=""):
        log("instantly workspace search did not find a known lead: CALL_FIRST history check unusable; stopping")
        return 2
    crm = load_crm(airtable)
    log(f"crm (read-only): {len(crm.emails)} emails, {len(crm.linkedins)} linkedin")
    william, flagged = william_exclusions(default_william_paths(args.william_dir))
    log(f"william lists: {len(william.emails)} emails, {len(william.linkedins)} linkedin, {len(william.phones)} phones, "
        f"{len(flagged)} flagged numbers")
    pilot = Pilot(cfg, store, apollo, instantly, units=units, units_by_opp=units_by_opp, approved=approved, core=core,
                  crm=crm, william=william, william_flagged=flagged, leads_by_email=leads, replied=replied,
                  bounced=bounced, unsubscribed=unsub, log=log)
    summary = pilot.run()
    members = store.members()
    n = write_call_file(os.path.join(args.out, "call_list_private.csv"), members)
    report = non_pii_report(members, summary, {
        "inputs": {"units": len(units), "approved_contacts": len(approved), "challenger_leads": len(leads),
                   "crm_emails": len(crm.emails), "william_people_emails": len(william.emails),
                   "william_phones": len(william.phones), "core_people": len(core.apollo_ids)},
        "cached_phones": {"core_people_with_personal_phone": 0,
                          "william_list_phones_reusable": 0, "note": "William-list people are excluded by rule"},
        "pilot_credit_cap": cfg.apollo_credit_cap,
    })
    write_report(os.path.join(args.out, "call_list_report.json"), report)
    store.finish_run(run_id, summary)
    log(json.dumps({"call_file_rows": n, **{k: report[k] for k in ("completed_by_cohort", "unique_people", "unique_phones")},
                    "ledger": summary["ledger"]}, default=str))
    return 0


def cmd_disposition(args) -> int:
    from .store import SidecarStore
    store = SidecarStore(args.state)
    store.record_disposition(args.person_key, args.disposition, notes=args.notes or "",
                             referral={"name": args.referral_name, "title": args.referral_title})
    print("recorded", args.disposition)
    return 0


def after_core(runner) -> int:
    """Isolation contract: whatever happens inside, the caller (the core's schedule)
    sees exit 0 and nothing blocks. The sidecar is skipped while the core holds its lock."""
    try:
        if not enabled():
            print(OFF_MESSAGE)
            return 0
        runner()
    except BaseException as exc:  # noqa: BLE001 -- a sidecar failure must never fail the core
        print(f"call-list sidecar failed and was contained: {type(exc).__name__}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tgtc_call_sidecar")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("pilot")
    pp.add_argument("--inputs", required=True)
    pp.add_argument("--out", required=True)
    pp.add_argument("--state", required=True)
    pp.add_argument("--william-dir", required=True)
    pp.add_argument("--credit-cap", type=int, default=2000)
    pp.add_argument("--target", type=int, default=100)
    pp.add_argument("--in-flight", type=int, default=8)
    dp = sub.add_parser("disposition")
    dp.add_argument("--state", required=True)
    dp.add_argument("--person-key", required=True)
    dp.add_argument("--disposition", required=True)
    dp.add_argument("--notes")
    dp.add_argument("--referral-name")
    dp.add_argument("--referral-title")
    sub.add_parser("after-core")
    args = p.parse_args(argv)
    if args.cmd == "after-core":
        return after_core(lambda: None)   # no scheduled work exists yet
    if not enabled():
        print(OFF_MESSAGE)
        return 0
    return {"pilot": cmd_pilot, "disposition": cmd_disposition}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
