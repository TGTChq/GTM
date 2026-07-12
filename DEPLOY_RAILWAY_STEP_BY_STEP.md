# TGTC Production Deployment — Railway

This package adds only deployment files. Copy them into the root of the current working project before pushing to GitHub.

## Final architecture

- `tgtc-daily-pipeline`
  - Start command: `python -u run_daily.py`
  - Cron: `0 14 * * *` (08:00 America/Merida; Railway cron is UTC)
  - Persistent volume: `/app/data/state`
- `tgtc-approved-sync`
  - Start command: `python -u run_approved.py`
  - Cron: `*/5 * * * *`
  - No volume needed

Both services use the same private GitHub repository and the same Railway Shared Variables.

## Before GitHub

1. Copy the deployment patch into the project root.
2. Confirm the CRM file exists:

```cmd
cd /d C:\TGTC\tgtc_pipeline
dir data\exclusions\crm_companies.csv
```

3. Confirm `.env` is ignored:

```cmd
git check-ignore -v .env
```

4. Do not continue if `.env` appears in `git status --short`.

## GitHub commands

Create an empty private repository in the TGTC GitHub organization/account. Do not initialize it with README, license, or gitignore.

Then run in CMD:

```cmd
cd /d C:\TGTC\tgtc_pipeline
git init -b main
git add .
git status --short
git commit -m "Production-ready TGTC outbound pipeline"
git remote add origin https://github.com/OWNER/tgtc-outbound-pipeline.git
git push -u origin main
```

Replace `OWNER` with the TGTC GitHub organization or Brett's GitHub username.

## Railway project

Create the project in Brett/TGTC's Railway workspace.

### Shared variables

Go to Project Settings -> Shared Variables and add the variables from `.env.example`, using the real values from the local `.env`.

Required secrets:

- RAPIDAPI_KEY
- APOLLO_API_KEY
- HUNTER_API_KEY
- AIRTABLE_TOKEN
- AIRTABLE_BASE_ID
- INSTANTLY_API_KEY

Required campaign variables:

- INSTANTLY_CAMPAIGN_GTM
- INSTANTLY_CAMPAIGN_ENGINEERING
- INSTANTLY_CAMPAIGN_MARKETING
- INSTANTLY_CAMPAIGN_CUSTOMER_SUCCESS
- INSTANTLY_CAMPAIGN_CUSTOMER_SUPPORT

Recommended runtime values:

- TZ=America/Merida
- PRODUCTION=1
- DATE_POSTED=3days
- NUM_PAGES=3
- REQUEST_TIMEOUT_SECONDS=45
- MIN_EMPLOYEES=25
- MAX_EMPLOYEES=1000
- VERIFY_WITH_HUNTER=1

Share the variables with both services.

## Service 1 — Daily Pipeline

1. New Project -> Deploy from GitHub repo.
2. Select the private TGTC repository.
3. Rename service to `tgtc-daily-pipeline`.
4. Settings -> Deploy -> Start Command:

```text
python -u run_daily.py
```

5. Settings -> Cron Schedule:

```text
0 14 * * *
```

6. Add a volume to this service only.
7. Mount path:

```text
/app/data/state
```

8. Attach all Shared Variables.

## Service 2 — Approved Sync

1. Add Service -> GitHub Repo -> select the same repository.
2. Rename service to `tgtc-approved-sync`.
3. Settings -> Deploy -> Start Command:

```text
python -u run_approved.py
```

4. Settings -> Cron Schedule:

```text
*/5 * * * *
```

5. Attach all Shared Variables.
6. Do not attach a volume.

## First production test

### Daily service

Temporarily set the cron to:

```text
*/5 * * * *
```

After one successful execution, restore:

```text
0 14 * * *
```

Success indicators in logs:

- `Pipeline completed successfully`
- scrape/filter/enrichment/Airtable steps completed
- no traceback
- a run summary path is printed

### Approved service

Leave one safe test lead as `Approved` in Airtable. The service should process it on the next run.

Success indicators:

- `approved: 1`
- `enrolled: 1` or a safe duplicate result
- `failed: 0`
- Airtable status becomes `Enrolled`

## Final go-live

Do not activate Instantly campaigns until:

- Brett approves the copy and launch
- sending accounts are assigned
- schedules/timezones in Instantly are correct
- daily and approved Railway services have each completed successfully
- the RapidAPI production key belongs to Brett/TGTC's paid JSearch plan
- exposed credentials have been rotated

## Rollback

If a bad code change is pushed:

1. Open the affected Railway service.
2. Open Deployments.
3. Redeploy the last known-good deployment, or revert the GitHub commit and push again.

## Normal operating workflow

1. Railway runs `run_daily.py` each morning.
2. Qualified leads appear in Airtable as `Pending`.
3. Human reviews `Ready to Approve` and `Review Queue`.
4. Human changes good leads to `Approved`.
5. Railway runs `run_approved.py` every five minutes.
6. Approved leads enter the correct Instantly campaign and become `Enrolled` in Airtable.
