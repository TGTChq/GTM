# Weekly reporting (core)

The weekly report for the rebuilt pipeline. It is measured from the **core database** --
the provider and delivery receipts the production runs write themselves -- so it needs
no run artifacts, survives the container that produced them, and costs nothing to
produce: generating a report makes no provider call.

It replaces the legacy file-based layer (`weekly_report/`, `run_weekly_report.py`) for
the rebuilt pipeline. That layer is **not running**: the `GTM` and `GTM Approved Sync`
services have no cron schedule, GTM's start command does not mention it, and the run
artifacts it reads live on a volume the exited cron container no longer exposes. It is
left in place, untouched, as the record of how the 2026-08/09 reports were produced.

## The week

**Friday 00:00 to the following Friday 00:00, America/Los_Angeles, end exclusive.**

That definition is not invented here. It is the one recorded in `WEEKLY_REPORTING.md`
and re-stated independently in the 2026-09-09 recovery note behind the workbooks that
were actually delivered. `tgtc_core/reporting/window.py` re-implements it inside the
core package (which imports nothing from the legacy orchestrator), and
`tests_core/test_weekly_report.py` asserts the two agree instant for instant across 80
sampled moments, so they cannot drift apart silently.

* The window is anchored to the **local wall clock**, so Friday 00:00 Pacific is 07:00
  UTC in PDT and 08:00 UTC in PST. A week containing a DST change is 167 or 169 real
  hours, by design.
* It is **half-open**: a run finishing exactly at the boundary belongs to the next
  week, so consecutive reports can never count it twice.
* A report produced on Friday morning covers the week that just **closed**. It does not
  wait for Friday to finish and never reaches into the day it is written.
* The timezone resolver is recorded on every report (`zoneinfo:tzdata`, or the codified
  US federal rule where no tz database exists).

## What it measures

Three rules hold everywhere, because a report that breaks them is worse than none:

1. **Nothing is added across units.** Records, jobs, units (company x campaign),
   contacts, Airtable records and Instantly creations are different things. Loss
   reasons carry their own unit and sit beside the stage they belong to; they are never
   subtracted from a total.
2. **No evidence means `unknown`, never `0`.** A zero is a measured zero. A *missing
   production run* is reported as a named missing day, never as a day that produced
   nothing.
3. **The KPI is defined once.** "A genuinely net-new lead" is an Instantly `created`
   receipt whose campaign is the campaign the approval was routed to, counted by
   distinct person. Every view of it -- weekly, per campaign, per day, cumulative --
   uses that one predicate, so an `existing` answer, a rejection, a Control campaign
   and the same person twice are excluded by construction.

Sections, each with its definitions carried in the JSON:

| Section | What it answers |
|---|---|
| `runs` | which runs the window contains, which local days had none |
| `acquisition` | Fantastic requests, records returned, unique ids, duplicate pages, records billed (confirmed vs estimate, never mixed) |
| `jobs` | new unique jobs, reviewed, qualified, rejected (by reason), no campaign fit, pending review |
| `units` | new employers, new company x campaign units, units worked, units closed by reason |
| `contacts` | candidates found, enriched, emails verified, approved, rejected by gate, compliance-blocked by reason, suppressions |
| `delivery` | genuine Instantly creations, already-existing, rejected by reason, Control creations, Airtable records, blocked and failed deliveries by reason |
| `backlog` | this week's own production separated from creations made from earlier approvals, and approvals carried out |
| `spend` | per-provider requests and credits, credits per final lead, dollars **only** where a unit price is recorded |
| `by_campaign` | all nine campaigns, always present, with measured zeros |
| `daily` | per local day in the report's own timezone |
| `previous_week` | the same headline metrics one week earlier, with the change |
| `cumulative` | since launch to the data cutoff -- labelled as a different view, never merged with the week |
| `legacy_airtable_review` | Airtable records with no genuine creation behind them: a review population, never counted, archived or removed |
| `reconciliation` | Airtable records = genuine creations + records written without one, checked rather than asserted |
| `alerts` / `notes` | graded: an alert needs a person, a note is worth knowing |

**The phone sidecar is not in any of it.** It keeps its own SQLite store and writes to
none of these tables; `source.sidecar_included` says so on every report, and a test
asserts the reporting package never so much as mentions it.

## Privacy

The readable summary contains **counts, definitions and named reasons only** -- no
name, email, phone number or contact row -- so it can be pasted into a channel or
forwarded as it stands. A test builds a report over rows that do contain personal data
and asserts none of it reaches the text.

The lead-level detail is a separate, deduplicated CSV (`--lead-export`) that traces each
lead to its job, employer, person, campaign and both delivery receipts. It refuses to be
written inside the repository, so personal data cannot be committed by accident; it
belongs beside the other private evidence in `C:\TGTC\call_sidecar_private\`.

## Idempotency

`report_id` is derived from the window's local start date (`weekly-2026-09-18`), so
every retry addresses the same row.

* Re-measuring a week is always safe and never clears its delivery record.
* A week is **delivered at most once**, however many times the job fires. A second
  attempt returns `already_delivered` and sends nothing; `--resend` is explicit and
  never automatic.
* The delivery row is written only after the provider accepted the message. A crash
  between the POST and the write re-sends next time: a duplicate report is recoverable,
  a silently skipped one is not.
* A **partial** (week-in-progress) report is never delivered at all.
* Reports are stored in `report_runs` with their payload, the data cutoff, the
  generation time and the flags, so the numbers stay readable exactly as they were sent
  even after the underlying rows are pruned.

## Commands

Generating spends nothing and writes nothing but its own row.

```bash
python -m tgtc_core weekly-report --week last                 # the closed week
python -m tgtc_core weekly-report --week current              # the week in progress (partial)
python -m tgtc_core weekly-report --week 2026-09-11           # a named week, re-issued unchanged
python -m tgtc_core weekly-report --week last --weeks-back 1  # the week before the closed one
```

Useful flags: `--out-dir DIR` (writes `<report_id>.json` and `.txt`, the JSON carrying
the SHA-256 of the text), `--lead-export PATH` (the private CSV), `--no-store` (pure
read), `--print-format {text,json,none}`, `--target N` (the per-run minimum a day is
flagged against), `--now ISO` (rehearsal), `--fail-on-alerts` (exit 4 on an alert; a
note never fails a run), `--if-due-hour H` (do nothing before that hour, evaluated in
the report's timezone, not UTC).

Delivery is deliberately hard to do by accident:

```bash
python -m tgtc_core weekly-report --week last \
  --send slack --confirm-destination '#the-confirmed-channel'
```

It refuses without `--confirm-destination`, refuses when the webhook variable is not
set on the service, and refuses to send a week that was already delivered.

## Deployment

The reporting job runs as its **own Railway service**, so nothing about the daily
production cron changes:

* Service: **GTM Weekly Report** (project `898f2e3a`, environment `production`), built
  from `Dockerfile.core` on branch `feat/rebuild-core` -- the same image as the core,
  which already copies `tgtc_core/`.
* Variables: `TGTC_DATABASE_URL` referencing Postgres Core, and nothing else. **No
  provider API keys**: the service cannot spend even if it were given the wrong command.
* Start command: `python -u -m tgtc_core weekly-report --week ${TGTC_REPORT_WEEK:-auto}
  --print-format text --fail-on-alerts`.
* Schedule: daily at 13:00 UTC. `--week auto` produces the **closed week** on the
  delivery weekday in Pacific and the **week in progress** on every other day, so a
  problem is visible days before Friday rather than on it.
* Alerting: an alert makes the run exit non-zero, which Railway shows as a failed
  execution; the same alerts are stored on the report row and printed at the top of the
  text.
* Rollback: set the service's `cronSchedule` to `null` (it stops running; nothing else
  in the project depends on it) or delete the service. The core service, its cron and
  its start command are untouched by any of this.

`migration 013` adds `report_runs`. The reporting command creates only that table
(`store.ensure_schema`) and never migrates anything else; the nightly `run-daily`
applies the migration in the ordinary way.

## Still missing, and deliberately not guessed

Two facts could not be established from any record in this project, and the pipeline
does not invent them:

1. **The exact requested Friday delivery time and timezone.** The window is documented;
   the delivery deadline is not. The closest evidence is the legacy GTM cron `0 14 * * *`
   (07:00 PDT) described as running "before the 08:00 Pacific meeting".
2. **The recipients and the destination.** Brett appears throughout as the report's
   reader (`acceptance/BRETT_REPORT_2026-09-06.md`,
   `acceptance/BRETT_RETROSPECTIVE_2026-W36.md`). **Roman appears nowhere** as a
   recipient of anything. A Slack webhook variable (`SLACK_WEEKLY_REPORT_WEBHOOK_URL`)
   exists on the `GTM` service only, and which channel it points at cannot be read from
   the variable.

Until those are supplied, the job **generates and stores** the report every day and
**sends nothing**. Enabling delivery is one change once the answer is known: put the
webhook on the reporting service and add `--send slack --confirm-destination '<channel>'`
(and `--if-due-hour H`) to the start command.
