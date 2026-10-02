-- Moving a contact is not the same thing as writing to them, and the difference is the
-- whole point of this step.
--
-- The out-of-office follow-up works by moving the contact into a campaign whose sequence
-- is one step. `POST /leads/move` answers 200 with a background job that is still
-- pending, so even the move is only real once Instantly says the contact is there. The
-- email is a further event again, and it happens later, in that campaign's own send
-- window, decided by Instantly and not by us.
--
-- So two moments are recorded separately, and neither is inferred from the other:
--
--   moved_at  -- Instantly confirmed the contact is in the one-step campaign
--   sent_at   -- a message to that contact actually exists in that campaign
--
-- `lead_id` is the primary key, so a retry can never move or count the same contact
-- twice, and `sent_message_id` is Instantly's own id for the message, which is the
-- receipt: the claim "a follow-up was sent" can be checked against the provider.

CREATE TABLE IF NOT EXISTS followup_deliveries (
    lead_id          text PRIMARY KEY,
    approval_id      bigint NOT NULL REFERENCES approvals(id),
    email            text NOT NULL DEFAULT '',
    from_campaign    text NOT NULL DEFAULT '',
    to_campaign      text NOT NULL,
    moved_at         timestamptz NOT NULL DEFAULT now(),
    sent_message_id  text,
    sent_at          timestamptz,
    sent_subject     text,
    sent_from        text
);

CREATE INDEX IF NOT EXISTS followup_deliveries_approval_idx ON followup_deliveries (approval_id);
CREATE INDEX IF NOT EXISTS followup_deliveries_awaiting_idx ON followup_deliveries (moved_at) WHERE sent_at IS NULL;
