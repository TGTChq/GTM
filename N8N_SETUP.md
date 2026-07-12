# n8n scheduling

The corrected Python code keeps business logic, retries, idempotency, and API payloads in one place. n8n only needs to schedule the two entry points and alert on non-zero exits.

## Workflow A — daily sourcing pipeline

1. **Schedule Trigger**: once daily at the desired US-business-hours time.
2. **Execute Command** (self-hosted n8n):

```bash
cd /path/to/tgtc_pipeline && /path/to/python run_daily.py
```

3. Add an error branch that sends the command output to Slack/email when the exit code is non-zero.

This workflow stops at Airtable. No prospect is contacted automatically.

## Workflow B — approved lead enrollment

1. **Schedule Trigger**: every minute.
2. **Execute Command**:

```bash
cd /path/to/tgtc_pipeline && /path/to/python run_approved.py
```

3. Add an error alert for non-zero exits.

`run_approved.py`:

- fetches only records with `Status = Approved`;
- enrolls them in the routed Instantly campaign;
- uses Instantly duplicate guards;
- changes successful/duplicate records to `Enrolled`;
- changes failures to `Error` and writes the API error into the Airtable `Error` field.

## Windows Task Scheduler alternative

Use `run_daily.bat` once daily and `run_approved.bat` every minute. Both batch files resolve their directory dynamically, so they do not contain Luis's local hard-coded path.

## n8n Cloud note

The Execute Command node is generally a self-hosted pattern. For n8n Cloud, expose these scripts through a small authenticated HTTP service or reproduce the calls with HTTP Request nodes. Do not put API keys directly in workflow JSON exports; use n8n credentials or environment variables.
