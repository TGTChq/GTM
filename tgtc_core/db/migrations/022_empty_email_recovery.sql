-- The authorised queue for the ONE recovery email, one row per affected recipient.
--
-- On 2026-09-21 16,875 messages went out to 7,777 recipients with an empty subject and
-- no body. The authorisation for the repair is narrow and worth encoding rather than
-- remembering: ONE email, to the recipients that can still be revalidated, from a
-- separate single-step campaign, while the nine original campaigns and the OOO
-- follow-up stay paused and the v2 campaigns keep taking new production.
--
-- The two mistakes this table exists to make structurally impossible:
--
--   1. A SECOND recovery email to the same person. The primary key is the recipient, not
--      a lead or an attempt, and the trigger below refuses to clear `sent_at` or to walk
--      a sent row back to a sendable state. A replay of the enroller therefore cannot
--      produce a second send even if it believes the row is unsent.
--   2. A recipient counted as net-new. These people were already acquired, already
--      approved and already (badly) emailed. Nothing here writes to `approvals`,
--      `delivery_outbox` or Airtable, so a recovered contact cannot enter the daily
--      success metric or open a second Airtable row by changing campaign.
--
-- `verified_role` is NOT NULL and non-empty on purpose: the subject is "Your <role>
-- opening", so an empty value would reproduce the incident in a smaller font. It is
-- produced by the production renderer from a vacancy that was open when the row was
-- authorised, and the posting it came from is recorded beside it.
CREATE TABLE IF NOT EXISTS empty_email_recovery (
    email                text PRIMARY KEY,
    person_id            bigint,
    first_name           text NOT NULL,
    last_name            text,
    employer             text,
    employer_domain      text,
    function_key         text NOT NULL,
    verified_role        text NOT NULL,
    posting_id           text,
    posting_title        text,
    posting_is_original  boolean,
    original_campaign_id text,
    -- authorised -> reserved -> enrolled -> sent, plus the terminal withheld / revoked
    state                text NOT NULL DEFAULT 'authorised',
    state_reason         text NOT NULL DEFAULT '',
    authorised_at        timestamptz NOT NULL DEFAULT now(),
    reserved_at          timestamptz,
    enrolled_at          timestamptz,
    instantly_lead_id    text,
    sent_at              timestamptz,
    message_id           text,
    revoked_at           timestamptz,
    attempts             integer NOT NULL DEFAULT 0,
    last_error           text NOT NULL DEFAULT '',
    evidence             jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- One of our own addresses, enrolled first and alone. Real recipients are not
    -- enrolled until a test row has been SENT and its subject read back as the approved
    -- one, so "we checked the copy" is a state the code can require rather than a step a
    -- person is trusted to have done.
    is_internal_test     boolean NOT NULL DEFAULT false,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT empty_email_recovery_state_ck CHECK (
        state IN ('authorised', 'reserved', 'enrolled', 'sent', 'withheld', 'revoked')),
    CONSTRAINT empty_email_recovery_role_ck CHECK (
        state IN ('withheld', 'revoked') OR length(btrim(verified_role)) > 0),
    CONSTRAINT empty_email_recovery_name_ck CHECK (
        state IN ('withheld', 'revoked') OR length(btrim(first_name)) > 0)
);

CREATE INDEX IF NOT EXISTS empty_email_recovery_state_idx
    ON empty_email_recovery (state, authorised_at);
-- One Instantly lead belongs to at most one recipient, so a mis-bound id is a loud
-- failure rather than two rows quietly sharing a send.
CREATE UNIQUE INDEX IF NOT EXISTS empty_email_recovery_lead_uq
    ON empty_email_recovery (instantly_lead_id) WHERE instantly_lead_id IS NOT NULL;

CREATE OR REPLACE FUNCTION empty_email_recovery_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.sent_at IS NOT NULL THEN
        IF NEW.sent_at IS NULL OR NEW.sent_at <> OLD.sent_at THEN
            RAISE EXCEPTION 'empty_email_recovery: sent_at is written once (%)', OLD.email;
        END IF;
        IF NEW.state NOT IN ('sent', 'revoked') THEN
            RAISE EXCEPTION 'empty_email_recovery: % was already sent, cannot become %',
                OLD.email, NEW.state;
        END IF;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS empty_email_recovery_guard_trg ON empty_email_recovery;
CREATE TRIGGER empty_email_recovery_guard_trg BEFORE UPDATE ON empty_email_recovery
    FOR EACH ROW EXECUTE FUNCTION empty_email_recovery_guard();
