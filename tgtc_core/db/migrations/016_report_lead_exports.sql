-- The week's lead-level detail, kept where the data already lives.
--
-- One row per reporting week, holding the file itself: the same private, authenticated
-- store as the leads it describes, so publishing the detail never means copying personal
-- data into a second place before somebody has verified who can read it. The row count
-- and checksum are stored beside it, because a detail file that does not reconcile with
-- the headline figure is worse than no file at all.
--
-- Idempotent by construction: one row per report_id. A retry replaces the content of the
-- week it belongs to and can never create a second file for it.
CREATE TABLE IF NOT EXISTS report_lead_exports (
    report_id     text PRIMARY KEY,
    window_start  timestamptz NOT NULL,
    window_end    timestamptz NOT NULL,
    row_count     integer NOT NULL,
    columns_json  jsonb NOT NULL,
    csv_gzip      bytea NOT NULL,
    sha256        text NOT NULL,
    generated_at  timestamptz NOT NULL DEFAULT now(),
    -- Where the file was published, once a destination and its readers are verified.
    -- NULL means the detail exists but has not been shared: the Slack summary then says
    -- the detail is pending rather than linking to nothing.
    published_url text,
    published_at  timestamptz,
    published_to  jsonb
);

-- The link the weekly Slack message carries, recorded per week rather than guessed.
ALTER TABLE report_runs ADD COLUMN IF NOT EXISTS detail_url text;
