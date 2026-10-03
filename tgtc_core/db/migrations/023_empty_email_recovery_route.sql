-- How each recipient reaches the recovery campaign: by moving an existing record, or by
-- creating a new one.
--
-- This is not a detail. Measured 2026-10-03 with our own mailbox: a move relocates a
-- stored contact and changes total storage by ZERO (the recovery campaign went 1 -> 0 and
-- the hold list 1,975 -> 1,976, counted with the endpoints that answer immediately rather
-- than `/campaigns/analytics`, which did not move at all for over a minute). The lead
-- keeps its id, its address and its data.
--
-- So of the 5,934 authorised recipients, 5,892 are already stored -- 4,555 in the nine
-- paused campaigns and 1,337 on the incident hold list -- and need no new storage at all.
-- Only the 42 whose record rotation already removed need a create, and a create does need
-- a slot. Any statement that the recovery needs 5,934 slots is wrong by 5,892.
--
-- `source_kind` is what `/leads/move` has to be told: it takes `campaign` for a lead in a
-- campaign and `list_id` for one parked on a list, and the two are not interchangeable.
ALTER TABLE empty_email_recovery
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'unknown';
ALTER TABLE empty_email_recovery
    ADD COLUMN IF NOT EXISTS source_id text;
-- The lead's own state in Instantly when the route was recorded. A bounced address is
-- not a candidate for a repair email: the first one never arrived, and sending to it
-- again spends our own deliverability to no purpose. 246 of the authorised recipients
-- turned out to be bounced, which our `outcome_events` did not know because the bounce
-- was recorded by the provider and not by us.
ALTER TABLE empty_email_recovery
    ADD COLUMN IF NOT EXISTS source_lead_status integer;
-- The record as Instantly held it before we touched it, so a move is reversible from
-- production and not only from a file on somebody's disk.
ALTER TABLE empty_email_recovery
    ADD COLUMN IF NOT EXISTS source_backup jsonb;

ALTER TABLE empty_email_recovery DROP CONSTRAINT IF EXISTS empty_email_recovery_route_ck;
ALTER TABLE empty_email_recovery ADD CONSTRAINT empty_email_recovery_route_ck CHECK (
    source_kind IN ('unknown', 'campaign', 'list', 'absent'));

-- A move needs something to move: a route that claims one must name the lead and its
-- source, or it is not a route.
ALTER TABLE empty_email_recovery DROP CONSTRAINT IF EXISTS empty_email_recovery_movable_ck;
ALTER TABLE empty_email_recovery ADD CONSTRAINT empty_email_recovery_movable_ck CHECK (
    source_kind NOT IN ('campaign', 'list')
    OR (instantly_lead_id IS NOT NULL AND source_id IS NOT NULL));

CREATE INDEX IF NOT EXISTS empty_email_recovery_route_idx
    ON empty_email_recovery (source_kind, state);
