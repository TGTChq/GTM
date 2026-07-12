# TGTC AI-Powered Job Intent & Outbound Pipeline

## Flow

`JSearch -> role relevance -> business filters -> independent audit -> Apollo/Hunter -> Airtable review -> Instantly`

`run_daily.py` performs Steps 1-4 and stops at the human review queue. `run_approved.py` polls approved Airtable records and enrolls them in Instantly.

## Important behavior

- A posting returned by multiple role searches is scored against every role and assigned to the strongest match. It is no longer labeled by whichever search returned it first.
- Clear role mismatches are rejected; ambiguous matches remain visible for human review.
- Staffing-description detection handles negative language such as “no staffing agencies” instead of treating it as proof that the employer is a staffing firm.
- Cross-day seen-state is committed only after Airtable is successfully updated. A failed downstream run no longer makes valid jobs disappear permanently.
- Apollo search uses the documented array query parameters and validates returned employer domains.
- Apollo person identity is preserved when no email is returned, allowing Hunter fallback to work.
- Customer Success and Customer Support use customer-function leaders rather than the generic revenue bucket.
- Automation Specialist jobs are routed dynamically: GTM/CRM/revenue automation goes to the GTM campaign and technical/AI automation goes to Engineering.
- A deterministic `Role Focus` extractor turns explicit JD signals into a controlled, grammatically safe fragment for the email. Raw JD sentences are never pasted into copy.
- One lead is produced per company + functional bucket, preventing the same CMO from being contacted three times for three marketing openings.
- Airtable insertion is idempotent through `Lead Key`.
- Instantly receives the hiring intent signal (`open_role`, `role_focus`, role bucket, job URL, posting date, company size) and uses duplicate guards.
- The staffing 95% criterion is not faked by comparing the rule to itself. A separate labeled validation CSV is supported.

## Setup

1. Create a virtual environment and install requirements.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

2. Keep your existing `.env`, add any variables missing from `.env.example`, and do not commit it.

3. Put Brett's CRM spreadsheet into `data/exclusions/crm_companies.csv`. The file must have a recognizable company column such as `company_name`, `company`, `account`, or `organization`.

4. Update the Airtable table using `AIRTABLE_SETUP.md`. During review, confirm that `Role Focus` reads naturally after the words “focused on.” Records with no supported focus are marked `manual_required` and must be edited before approval.

5. Configure at least one Instantly campaign ID. A single `INSTANTLY_CAMPAIGN_ID` works as a fallback; bucket and size-specific IDs are supported.

6. Run static tests:

```bash
python -m unittest discover -s tests -v
python validate_setup.py
```

7. Run a controlled live test. This consumes Apollo/Hunter credits for the number of companies requested:

```bash
python run_full_test.py --companies 3 --push-airtable
```

8. Run production Steps 1-4:

```bash
python run_daily.py
```

9. Schedule the approval worker every minute from n8n or Task Scheduler:

```bash
python run_approved.py
```

## Proving the 95% staffing criterion

Generate a random sample from real scraped jobs:

```bash
python build_staffing_validation_sample.py --sample-size 200
```

Open `data/validation/staffing_ground_truth.csv`, manually label `is_staffing` as `yes` or `no`, then run another daily pipeline or call the audit directly. The report will calculate accuracy, precision, recall, and F1 against human labels.

## What still requires a real run

No code-only review can prove external API permissions, plan entitlements, data coverage, or match quality. Use `validate_setup.py --live` and a 3-company controlled run. Then inspect:

- `logs/`
- `logs/runs/`
- `data/raw/`
- `data/filtered/`
- `data/enriched/`

The code is designed to fail with explicit diagnostics rather than silently continuing with malformed API responses.

## How `role_focus` works

`role_focus.py` uses role-specific pattern maps and canonical phrases. It searches only the actual job title and description, ranks explicit signals, and returns at most three concise themes. For example:

```text
JD signals: HubSpot workflows + Clay enrichment + lead routing
role_focus: CRM automation, lead enrichment, and lead routing
```

This approach is intentionally deterministic for the paid test: no extra model/API cost, no hallucinated responsibilities, stable grammar, and an auditable evidence field in Airtable. The human reviewer can edit the suggestion. If no supported signal is found, the field stays blank and the lead cannot be enrolled until a reviewer supplies a factual focus.
