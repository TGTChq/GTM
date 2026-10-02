-- A run that was killed leaves a start with no end, and nothing can ever close it.
--
-- On 2026-09-24 a push to the deployment branch replaced the container mid-run: run
-- 20260924T030130.689454Z-91bada58 logged `daily/start` and died ten seconds later. The
-- work was done instead by a later run that completed. But the weekly report's readiness
-- gate counts "started and never ended" as work still in flight, so a single dead run
-- holds Friday's report hostage for ever.
--
-- Writing a fake `daily/end` would fix the symptom by lying: the run did not finish, and
-- a future reader would believe it did. So the incident is recorded as what it was --
-- interrupted, by what, and which run did the work instead -- and the gate is taught to
-- tell an incident apart from work in flight.
--
-- The record is a CLAIM. It never dismisses anything on its own: the gate re-checks the
-- proofs at report time and keeps blocking if any of them stops holding.

CREATE TABLE IF NOT EXISTS run_incidents (
    run_id         text PRIMARY KEY,
    kind           text NOT NULL,
    -- The run that did the work instead. It must itself have completed, and it must have
    -- started after the interrupted one; that is what proves the container is gone,
    -- because the run lock is exclusive and a later run took and released it.
    superseded_by  text NOT NULL,
    -- What was checked when the incident was filed, kept verbatim for a later reader.
    evidence       jsonb NOT NULL DEFAULT '{}'::jsonb,
    note           text NOT NULL DEFAULT '',
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    recorded_by    text NOT NULL DEFAULT '',
    CHECK (run_id <> superseded_by)
);

CREATE INDEX IF NOT EXISTS run_incidents_superseded_idx ON run_incidents (superseded_by);
