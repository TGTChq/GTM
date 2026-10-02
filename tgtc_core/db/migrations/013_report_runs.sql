-- Weekly reporting state, in the database rather than on a container volume.
--
-- Two things the file-based legacy layer could not do, and the reason this table
-- exists: a report survives the container it was generated in (the 2026-08 artifacts
-- were unreachable once the cron container exited), and a RETRY can tell whether the
-- week was already delivered. ``report_id`` is derived from the window's local start
-- date, so re-running the same week always addresses the same row -- that is what
-- makes a retry safe rather than a second message to the reader.
--
-- The report payload is kept because the numbers must still be readable months later
-- exactly as they were sent, even after the underlying rows have been pruned.
CREATE TABLE IF NOT EXISTS report_runs (
    report_id        text PRIMARY KEY,
    kind             text NOT NULL CHECK (kind IN ('weekly', 'partial')),
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    timezone         text NOT NULL,
    data_cutoff      timestamptz NOT NULL,
    generated_at     timestamptz NOT NULL DEFAULT now(),
    payload_json     jsonb NOT NULL,
    flags_json       jsonb NOT NULL DEFAULT '[]'::jsonb,
    status           text NOT NULL DEFAULT 'ok',
    -- Delivery is recorded only when a provider actually accepted the message.
    delivery_target  text,
    delivered_at     timestamptz,
    delivery_receipt jsonb,
    attempts         integer NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS report_runs_window_idx ON report_runs (window_start DESC);
