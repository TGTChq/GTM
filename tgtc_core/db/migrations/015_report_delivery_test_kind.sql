-- A connectivity test is a delivery too, and it is governed by the same rule.
--
-- Before a weekly report is trusted to a channel, one short labelled message proves the
-- credential reaches it. Recording that message in the same table as the report means
-- the proof is durable, the receipt is auditable, and the test itself demonstrates the
-- property the Friday retries depend on: a second attempt with the same key sends
-- nothing.
ALTER TABLE report_deliveries DROP CONSTRAINT IF EXISTS report_deliveries_kind_check;
ALTER TABLE report_deliveries ADD CONSTRAINT report_deliveries_kind_check
    CHECK (kind IN ('final', 'status_notice', 'connectivity_test'));
