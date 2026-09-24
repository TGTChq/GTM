-- When a row was FIRST recorded, separate from when it was last touched.
--
-- ``candidate_attempts``, ``classifications`` and ``evidence`` are upserted: a replay of
-- the same candidate, posting or fact overwrites the row AND sets ``created_at = now()``.
-- That makes ``created_at`` a "last attempt" timestamp wearing the name of a creation
-- date, and it moves history: measured 2026-09-24, the 2026-09-24 production run
-- re-stamped 2,352 attempts belonging to units opened days earlier, which silently moved
-- three people out of the closed week 2026-09-18 -> 2026-09-23 (595 -> 592) AFTER that
-- week had been reported.
--
-- ``first_recorded_at`` is written once and never updated, because an upsert only assigns
-- the columns its DO UPDATE names. Every windowed measurement reads it, so a closed week
-- cannot change after the fact.
--
-- WHAT CANNOT BE RECONSTRUCTED: for rows that already existed, the backfill can only copy
-- today's ``created_at``. Where a row had already been re-stamped, its original recording
-- date is gone -- the value was overwritten in place and no history table kept it. Weeks
-- reported before this migration therefore stay as their stored report payload says; that
-- artifact, not a re-measurement, is the record.
ALTER TABLE candidate_attempts ADD COLUMN IF NOT EXISTS first_recorded_at timestamptz;
ALTER TABLE classifications    ADD COLUMN IF NOT EXISTS first_recorded_at timestamptz;
ALTER TABLE evidence           ADD COLUMN IF NOT EXISTS first_recorded_at timestamptz;

UPDATE candidate_attempts SET first_recorded_at = created_at WHERE first_recorded_at IS NULL;
UPDATE classifications    SET first_recorded_at = created_at WHERE first_recorded_at IS NULL;
UPDATE evidence           SET first_recorded_at = created_at WHERE first_recorded_at IS NULL;

ALTER TABLE candidate_attempts ALTER COLUMN first_recorded_at SET DEFAULT now();
ALTER TABLE classifications    ALTER COLUMN first_recorded_at SET DEFAULT now();
ALTER TABLE evidence           ALTER COLUMN first_recorded_at SET DEFAULT now();

CREATE INDEX IF NOT EXISTS candidate_attempts_first_recorded_idx ON candidate_attempts (first_recorded_at);
CREATE INDEX IF NOT EXISTS classifications_first_recorded_idx ON classifications (first_recorded_at);
