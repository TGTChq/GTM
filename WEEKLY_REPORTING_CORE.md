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

## Delivery (confirmed 2026-09-23)

**Destination:** the Slack channel `#gtm-engineering`.
**Moment:** every Friday at **06:00 America/Los_Angeles**.
**If the week has not closed:** retry until **07:00 local**; if it still has not closed,
post a clearly labelled status notice -- never partial numbers dressed as the final
report -- and send the real report as soon as the data does close.

One tick does exactly one of three things (`reporting/schedule.decide`):

| clock | data closed? | what happens |
|---|---|---|
| any other day, or before 06:00 | — | nothing; the week in progress is measured and stored |
| 06:00–07:00 | yes | the report is published (once per channel) |
| 06:00–07:00 | no | wait for the next tick |
| after 07:00 | no | the labelled status notice, once |
| after 07:00 | yes | the report is published |

**"Closed" is a fact about production, not about the hour.** A week is held back only
while something could still change it: a run inside the window that logged no end, a
production run holding the run lock, delivery rows still queued or in flight for this
window's approvals, or a reconciliation difference nothing explains. Things waiting
cannot fix -- a day that simply had no run, days before the database's coverage begins,
the historical Airtable records that already carry a named reason -- are reported and do
not hold the report back.

**The clock is read in the report's timezone.** 06:00 Pacific is 13:00 UTC in PDT and
14:00 UTC in PST; a schedule that only knew UTC would be an hour wrong for half the
year. The cron fires across a wider UTC band and every tick decides locally.

### Exceptions are graded

* **integrity** — a difference nothing explains, a lead created in a Control campaign, or
  a refused run. These fail the scheduled job (exit 4), which is how they reach a person.
  A day inside the coverage with **no run at all** is deliberately NOT here: it is a fact
  about the past that waiting cannot change, a correct reconciled report should carry it
  rather than be withheld for it, and a job that exits red every twenty minutes over a
  gap nobody can now fix teaches people to ignore red.
* **alert** — a fact the readers must see but nobody needs woken for: a missing run (such
  as 2026-09-19), a day below the 1,000 minimum, Airtable records written with a named
  reason behind them. Shown in the report and in Slack; never fails a job and never
  withholds one.
* **note** — true, worth printing, never a reason to act: an unverified unit price, days
  the database cannot speak about.

### Idempotency

Delivery is keyed by **(reporting week, channel, kind of message)** in
`report_deliveries`, so a retry every twenty minutes can never post the report twice.

* `report_id` comes from the window's local start date (`weekly-2026-09-18`).
* A second attempt after a successful one returns `already_delivered` and sends nothing;
  `--resend` is explicit and never automatic.
* The status notice and the final report are different messages: the notice never
  satisfies the report, and neither is posted twice.
* The receipt is written only after Slack accepted the message. A crash between the POST
  and the write re-sends next tick: a duplicate is recoverable, a silently skipped report
  is not.
* A **partial** (week-in-progress) report is never delivered at all.
* Reports are stored in `report_runs` with their payload, data cutoff, generation time
  and flags, so the numbers stay readable exactly as they were sent.

### The destination is verified, never inferred

With a bot token (`SLACK_BOT_TOKEN`, scopes `channels:read` + `chat:write`, app invited
to the channel) the channel is resolved **by name** against the workspace and the message
is addressed to that channel id; the receipt records `slack_api:C…`. An incoming webhook
URL is opaque -- nothing in it says where it posts and no API will tell you -- so that
path refuses to run unless an operator asserts the channel with
`--destination-basis webhook-declared`, and the receipt then records `webhook_declared`.
The URL is never printed, logged or stored.

## Commands

Generating spends nothing and writes nothing but its own row.

```bash
python -m tgtc_core weekly-report --week last                 # the closed week
python -m tgtc_core weekly-report --week current              # the week in progress (partial)
python -m tgtc_core weekly-report --week 2026-09-11           # a named week, re-issued unchanged
python -m tgtc_core weekly-report --week last --weeks-back 1  # the week before the closed one
```

Useful flags: `--out-dir DIR` (writes `<report_id>.json` and `.txt`, the JSON carrying the
SHA-256 of the text), `--lead-export PATH` (the private CSV), `--no-store` (pure read),
`--print-format {text,json,none}`, `--target N` (the per-run minimum a day is flagged
against), `--now ISO` (rehearsal), `--fail-on {integrity,alerts,never}`, `--detail-url`
(a link to the secure lead-level detail, when one is published).

Delivery is deliberately hard to do by accident:

```bash
python -m tgtc_core weekly-report --week last \
  --send slack --slack-channel '#gtm-engineering' \
  --delivery-weekday friday --due-hour 6 --retry-until-hour 7
```

It refuses without `--slack-channel`, refuses when no destination can be verified,
refuses to publish a week already delivered to that channel, and refuses to publish a
partial week at all. `--dry-run-send` renders the exact Slack message and sends nothing --
that is how the format is rehearsed against real data; the message is printed on a single
line, because a container's log lines arrive out of order.

## Deployment

The reporting job runs as its **own Railway service**, so nothing about the daily
production cron changes.

| | |
|---|---|
| Service | **GTM Weekly Report** `d5a09e27-b403-49c1-9555-75341010a004` (instance `2feae949-03a7-4520-8a73-c3d467f22dcb`), project `898f2e3a`, environment `production` |
| Source | `TGTChq/GTM`, branch `feat/rebuild-core`, built from `Dockerfile.core` -- the same image as the core, which already copies `tgtc_core/` |
| Variables | `TGTC_DATABASE_URL` → Postgres Core, `RAILWAY_DOCKERFILE_PATH`, and three optional hooks: `TGTC_REPORT_WEEK`, `TGTC_REPORT_ARGS`, `TGTC_REPORT_DESTINATION_ARGS`. **No provider API keys**: the service cannot spend. |
| Start command | `sh -ec 'python -u -m tgtc_core weekly-report --week ${TGTC_REPORT_WEEK:-auto} --send slack --slack-channel "#gtm-engineering" --delivery-weekday friday --due-hour 6 --retry-until-hour 7 --fail-on integrity --print-format none ${TGTC_REPORT_DESTINATION_ARGS:-} ${TGTC_REPORT_ARGS:-}'` |
| Schedule | `0,20,40 13-20 * * *` — every 20 minutes between 13:00 and 20:59 UTC, daily |
| Delivery moment | Friday **06:00 America/Los_Angeles** = 13:00 UTC in PDT, 14:00 UTC in PST. The cron covers both; each tick decides in local time |

Why that shape: the retries between 06:00 and 07:00 local need 20-minute granularity, the
later ticks catch a week that closes late in the morning, and running every day means a
problem is visible days before Friday rather than on it. On any day but Friday a tick
measures the week in progress, stores it and stops.

Alerting: an **integrity** problem exits 4, which Railway shows as a failed execution; a
delivery failure exits 5. Business alerts and notes never fail the job.

Rollback, in order of severity:

```bash
# stop the schedule, keep the service and its history
railway api 'mutation { serviceInstanceUpdate(serviceId: "d5a09e27-b403-49c1-9555-75341010a004", environmentId: "bae427bd-64a6-4f4e-8f56-fbd406985434", input: {cronSchedule: null}) }'
# or stop only the posting, keeping the measurements: clear TGTC_REPORT_DESTINATION_ARGS
# and remove SLACK_BOT_TOKEN from the service
```

Neither touches the core service, its cron, its start command or its image. Reverting the
reporting code is `git revert` of the reporting commits: the core pipeline imports nothing
from `tgtc_core/reporting/`.

Migrations **013** (`report_runs`) and **014** (`report_deliveries`) are applied by the
reporting command itself (`store.ensure_schema`, which touches only its own two tables)
and, in the ordinary way, by the nightly `run-daily`.

## Posting is active (2026-09-23)

The destination is a **new incoming webhook created for `#gtm-engineering`**, held on the
reporting service as `SLACK_WEEKLY_REPORT_WEBHOOK_URL` — a different credential from the
older webhook on the legacy `GTM` service (confirmed by comparing fingerprints, never
values). Because an incoming webhook cannot be introspected, the destination is recorded
as **declared** rather than resolved: `TGTC_REPORT_DESTINATION_ARGS=--destination-basis
webhook-declared`, and every receipt says so. Adding a `SLACK_BOT_TOKEN` later upgrades
the same job to a resolved-by-name channel id without any other change.

**The channel was proved before the first report.** `slack-test` posted exactly one short,
labelled connectivity test — what it is, when the report arrives, what a delay looks
like, and no pipeline numbers — and Slack accepted it:

```json
{"sent": true, "report_id": "connectivity-test-2026-09-23", "kind": "connectivity_test",
 "channel": "#gtm-engineering", "destination_basis": "webhook_declared",
 "receipt": {"transport": "incoming_webhook", "sent_at": "2026-09-23T07:27:40Z"}}
```

A second execution of the same command returned `already_delivered` and sent nothing —
the same guard the Friday retries rely on, proved live on the cheapest message there is.

### A Railway fact worth knowing

**Variables are baked into a deployment.** `variableCollectionUpsert` with
`skipDeploys: true` changes what the next *deployment* will see, not what an existing
one runs with: a cron execution of the current deployment keeps the values captured when
it was created. So after changing any `TGTC_REPORT_*` variable or the Slack credential,
redeploy (`serviceInstanceDeployV2`) or the change will not take effect. This was found
the hard way here — a rehearsal flag was set and silently ignored for two executions.

## Rehearsals against production (2026-09-23)

Both were run on the deployed service, read-only apart from their own `report_runs`
row, and spent nothing.

**A completed week -- `weekly-2026-09-11` (Sep 11 - Sep 17, 2026):** 0 genuine
Instantly creations and 0 Airtable records, 86 contacts approved, 3,548 new unique
jobs, 708 new units, 3,820 Fantastic records, 284 Apollo credits. Four alerts, all of
them "no production run recorded on <day>" for 09-11 to 09-14 -- correct: the core
database's oldest posting is 2026-09-16, and delivery started on 09-20. Reconciliation
closes at 0.

**The week in progress -- `partial-2026-09-18` (Sep 18 - Sep 22, to date):** 3,623
genuine Instantly creations against 3,840 Airtable records; 14,024 Fantastic records
returned; 13,356 new unique jobs; 14,317 reviewed (8,964 qualified, 5,348 rejected, 408
no campaign fit, 203 pending); 5,308 new employers; 5,728 new units; 12,922 candidates
found, 5,057 emails verified, 4,230 contacts approved, 476 compliance-blocked; 118
Instantly rejections (all `instantly_existing_other_campaign`) and 13 already existing;
1.43 Apollo credits per final lead. Reconciliation closes exactly: 3,840 = 3,623 + 217,
and the 217 are named. The lead-level export produced **3,623 rows -- one per person,
the same number as the KPI**.

### Why the 2026-09-23 run recorded 1,045 Airtable rows and 1,013 Instantly creations

Measured per approval, the 1,045 are exactly `1,013 + 30 + 2`:

| | approvals |
|---|---|
| Airtable record **and** a genuine Instantly creation | 1,013 |
| Airtable record, Instantly **rejected** -- the person was already in another campaign | 30 |
| Airtable record, Instantly **existing** -- already in this campaign | 2 |
| no delivery at all (blocked or undelivered) | 122 |

The run did **not** carry the Airtable gate. Deployment `cf759d02` (commit `9be626b`,
the gate) was created at 05:17Z on 2026-09-23, after the run finished at 05:11Z; the run
executed on `14a09232` (commit `d943d2b`). The receipts show it directly: in every one
of the 1,045 rows, including the 1,013 good ones, the Airtable receipt precedes the
Instantly one. From the next scheduled run onward the gate blocks those 32 rows with
their named reason.

## The 217 legacy Airtable records

The report recomputes this population from the database on every run rather than
quoting the audit: **217 records** with no genuine Instantly creation behind them --
118 `not_delivered:instantly_existing_other_campaign`, 86
`compliance:outreach_eligibility_unknown` (all 2026-09-18), 13 `existing`. They are
reported in their own section, are **not** counted as delivered leads anywhere, and are
neither archived nor removed by anything here. A person decides what happens to them;
the private list is `C:\TGTC\call_sidecar_private\airtable_rows_for_review_20260923.csv`.

## What the readers confirmed, 2026-09-23

* Destination: the Slack channel `#gtm-engineering`.
* Delivery: every Friday at 06:00 America/Los_Angeles; retry until 07:00 if the runs or
  receipts belonging to the window are still closing; a labelled status notice if it has
  not closed by then, and the real report when it does.
* The window is unchanged: Friday 00:00 to the following Friday 00:00
  America/Los_Angeles, end exclusive.

Brett is the only reader named in this project's own records
(`acceptance/BRETT_REPORT_2026-09-06.md`, `acceptance/BRETT_RETROSPECTIVE_2026-W36.md`);
Roman appears in none of them, which is why the channel -- not a list of names -- is the
destination the job holds.

## The Slack format, rehearsed against real data

`--dry-run-send` was run on the deployed service against the week in progress
(3,623 creations, 3,840 Airtable records) and the message was read back before anything
was activated. Thirteen blocks, in the order the readers asked for: window, cutoff and
status; jobs captured and reviewed with every percentage next to the count it came from;
qualified jobs, employers and company × campaign units; verified contacts, Airtable
records and genuine Instantly creations as three separate figures; the daily trend, the
previous week and all nine campaigns; provider consumption and cost per final new lead;
then short, explicit exceptions and where the lead-level detail lives. No name, email or
phone number appears anywhere in it.

Two things the first render showed, both fixed: the same 217 records were listed twice
without saying they were the same rows, and two campaign names were cut mid-word.

## The old webhook on the legacy GTM service

`SLACK_WEEKLY_REPORT_WEBHOOK_URL` on the `GTM` service is a **different** webhook and is
used by nothing:

* `GTM` and `GTM Approved Sync` have no cron, and both start commands only print
  `TGTC_PAUSED_PENDING_CREDITS`;
* no other service in the environment holds a Slack webhook at all (checked by
  fingerprint, so no value was read or printed);
* CI only asserts the variable is *absent*; it never posts;
* the only code that would use it, `weekly_report/slack.py` via
  `run_weekly_report.py --slack`, is not in the core image and nothing schedules it.

It is therefore safe to revoke in Slack. The only thing that stops working is a manual
`run_weekly_report.py --slack` from a laptop, which nothing depends on.
