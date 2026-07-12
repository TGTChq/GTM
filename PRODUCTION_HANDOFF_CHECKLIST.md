# TGTC Production Handoff Checklist

## Ownership

- [ ] Private GitHub repository belongs to TGTC/Brett
- [ ] Railway project belongs to TGTC/Brett
- [ ] Luis has collaborator access needed to deploy and troubleshoot
- [ ] Production API subscriptions belong to TGTC/Brett

## Security

- [ ] `.env` is not in GitHub
- [ ] RapidAPI key exposed during testing has been rotated
- [ ] Any other credential shared outside the owner's secure systems has been rotated
- [ ] Railway secrets are stored as variables, preferably sealed after validation

## Daily Pipeline

- [ ] Service name: `tgtc-daily-pipeline`
- [ ] Start command: `python -u run_daily.py`
- [ ] Cron: `0 14 * * *`
- [ ] Volume mounted at `/app/data/state`
- [ ] Latest run completed successfully
- [ ] New qualified leads reached Airtable

## Approved Sync

- [ ] Service name: `tgtc-approved-sync`
- [ ] Start command: `python -u run_approved.py`
- [ ] Cron: `*/5 * * * *`
- [ ] Test lead reached correct Instantly campaign
- [ ] Airtable status changed to `Enrolled`
- [ ] Custom variables populated in Preview

## Instantly

- [ ] Four campaign sequences saved
- [ ] `{{open_role}}` populated
- [ ] `{{role_focus}}` populated
- [ ] `{{accountSignature}}` used
- [ ] Sending accounts assigned
- [ ] Sending schedules/timezones checked
- [ ] Campaigns remain Draft until Brett authorizes launch

## Final acceptance evidence

- [ ] Screenshot: Railway daily service successful execution
- [ ] Screenshot: Railway approved sync successful execution
- [ ] Screenshot: Airtable Ready to Approve / Enrolled views
- [ ] Screenshot: Instantly lead with custom variables
- [ ] Screenshot: Instantly Preview with rendered copy
- [ ] Latest controlled-test metrics documented
