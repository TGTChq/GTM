-- Budget namespaces (2026-09-22). A budget belongs to exactly one KIND of run:
-- `prod-scheduled-YYYYMMDD` (the daily cron and its retries), `prod-manual-<ts>`,
-- `canary-<ts>`, `call-sidecar-<ts>`. Measured 2026-09-22: a manual run consumed
-- `prod-core-20260922`, so the scheduled run that owned that day got 0 Fantastic
-- records and continued silently. The first run to use a budget claims it for its
-- kind; a run of any other kind is refused before it can reserve anything.
CREATE TABLE IF NOT EXISTS budget_claims (
    budget_id     text PRIMARY KEY REFERENCES spend_budgets(budget_id),
    kind          text NOT NULL CHECK (kind IN ('scheduled', 'manual', 'canary', 'sidecar')),
    first_run_id  text NOT NULL,
    last_run_id   text NOT NULL,
    runs          integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
