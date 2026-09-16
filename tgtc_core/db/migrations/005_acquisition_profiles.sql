-- Additive metadata: old windows and cursors remain legacy_v1 and are never reset.
-- complete_coverage describes ONLY the partition's saved query, not the market.
ALTER TABLE source_partitions ADD COLUMN IF NOT EXISTS query_profile text NOT NULL DEFAULT 'legacy_v1';
ALTER TABLE source_partitions DROP CONSTRAINT IF EXISTS source_partitions_source_lane_window_start_window_end_key;
CREATE UNIQUE INDEX IF NOT EXISTS source_partitions_scope_uq
    ON source_partitions(source, lane, window_start, window_end, query_profile);
