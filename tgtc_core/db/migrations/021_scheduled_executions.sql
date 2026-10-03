-- One authorised scheduled execution per day, claimed atomically and durably.
--
-- The run lock stops two runs OVERLAPPING; it says nothing about a second run
-- that starts after the first released it. A deployment starts a cron service's
-- command, so on 2026-10-02 two deploys produced two sequential runs on the same
-- day's budget. The UTC-hour window narrows that but a redeploy between 03:00 and
-- 05:59Z is inside the window, so the claim has to outlive the process.
CREATE TABLE IF NOT EXISTS scheduled_executions (
    execution_day text PRIMARY KEY,
    run_id        text NOT NULL,
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    -- NULL means claimed but never closed: the container died mid-run. That is
    -- INTERRUPTED, which is not the same thing as completed, and a recovery must
    -- be authorised by name rather than assumed.
    finished_at   timestamptz,
    outcome       text,
    attempt       integer NOT NULL DEFAULT 1,
    recovery_of   text,
    updated_at    timestamptz NOT NULL DEFAULT now()
);
