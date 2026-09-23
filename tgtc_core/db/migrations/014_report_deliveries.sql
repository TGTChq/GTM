-- One delivery per reporting week PER DESTINATION.
--
-- ``report_runs.delivered_at`` answered "was this week sent?" for a single channel.
-- The delivery rule is now per week AND per channel, and a week can legitimately
-- produce two DIFFERENT messages: the final report, and -- when the data has not
-- closed by the end of the retry window -- a labelled status notice saying so. Each
-- is sent at most once, which is what makes an automatic retry safe: a retry that
-- posts the report twice is worse than one that posts it late.
CREATE TABLE IF NOT EXISTS report_deliveries (
    report_id     text NOT NULL,
    channel       text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('final', 'status_notice')),
    delivered_at  timestamptz NOT NULL DEFAULT now(),
    -- How the destination was established: 'slack_api:<channel_id>' means the channel
    -- id was resolved and confirmed against the workspace; 'webhook_declared' means an
    -- operator asserted it, because an incoming webhook URL cannot be introspected.
    destination_basis text NOT NULL DEFAULT 'unverified',
    receipt       jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts      integer NOT NULL DEFAULT 1,
    PRIMARY KEY (report_id, channel, kind)
);

CREATE INDEX IF NOT EXISTS report_deliveries_time_idx ON report_deliveries (delivered_at DESC);
