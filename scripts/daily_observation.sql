-- The daily table, from production. Read-only.
--
-- Newly produced is kept apart from backlog delivered: a creation counts as "new" only
-- when the approval behind it was made by that same UTC day's run. Everything else is
-- backlog being drained, however good it is.
--
--   psql ... -X -A -F '|' -f scripts/daily_observation.sql
WITH days AS (
    SELECT generate_series((now() - interval '8 days')::date, now()::date, interval '1 day')::date AS d
),
approved AS (
    SELECT date_trunc('day', approved_at)::date AS d, count(*) AS n
    FROM approvals WHERE state <> 'revoked' AND approved_at IS NOT NULL GROUP BY 1
),
creations AS (
    SELECT date_trunc('day', r.received_at)::date AS d,
           count(*) FILTER (WHERE date_trunc('day', a.approved_at)::date = date_trunc('day', r.received_at)::date) AS fresh,
           count(*) FILTER (WHERE date_trunc('day', a.approved_at)::date < date_trunc('day', r.received_at)::date) AS backlog,
           count(*) AS total
    FROM delivery_receipts r
    JOIN delivery_outbox o ON o.id = r.outbox_id
    JOIN approvals a ON a.id = o.approval_id
    WHERE r.channel = 'instantly' AND r.receipt_kind = 'created'
    GROUP BY 1
),
airtable AS (
    SELECT date_trunc('day', received_at)::date AS d, count(*) AS n
    FROM delivery_receipts WHERE channel = 'airtable' AND receipt_kind IN ('created', 'reconciled') GROUP BY 1
),
spend AS (
    SELECT date_trunc('day', created_at)::date AS d,
           round(sum(estimated_credits) FILTER (WHERE provider = 'fantastic')::numeric, 0) AS fantastic,
           round(sum(estimated_credits) FILTER (WHERE provider = 'apollo')::numeric, 1) AS apollo
    FROM spend_reservations WHERE status <> 'refused' GROUP BY 1
),
blocked AS (
    SELECT date_trunc('day', a.approved_at)::date AS d, count(*) AS n
    FROM approvals a WHERE a.state <> 'revoked' AND a.outreach_eligible IS NOT TRUE GROUP BY 1
)
SELECT 'day|' || days.d
    || '|approved=' || coalesce(approved.n, 0)
    || '|new_created=' || coalesce(creations.fresh, 0)
    || '|backlog_created=' || coalesce(creations.backlog, 0)
    || '|instantly_total=' || coalesce(creations.total, 0)
    || '|airtable=' || coalesce(airtable.n, 0)
    || '|compliance_blocked=' || coalesce(blocked.n, 0)
    || '|fantastic=' || coalesce(spend.fantastic, 0)
    || '|apollo=' || coalesce(spend.apollo, 0)
FROM days
LEFT JOIN approved ON approved.d = days.d
LEFT JOIN creations ON creations.d = days.d
LEFT JOIN airtable ON airtable.d = days.d
LEFT JOIN spend ON spend.d = days.d
LEFT JOIN blocked ON blocked.d = days.d
ORDER BY days.d;

-- Per campaign, for the reconciliation the mandate asks for (current window only).
SELECT 'campaign|' || c || '|' || n FROM (
    SELECT coalesce(external_campaign, '(none)') AS c, count(*) AS n
    FROM delivery_receipts
    WHERE channel = 'instantly' AND receipt_kind = 'created' AND received_at >= now() - interval '2 days'
    GROUP BY 1) t ORDER BY 1;

-- What is still waiting, and why.
SELECT 'waiting|' || ch || '|' || st || '|' || note || '|' || n FROM (
    SELECT channel AS ch, state AS st, coalesce(left(last_error, 40), '-') AS note, count(*) AS n
    FROM delivery_outbox WHERE state <> 'delivered' GROUP BY 1, 2, 3) t ORDER BY 1;

-- Replacement work, honestly reported.
SELECT 'work|' || k || '|' || st || '|' || why || '|' || n FROM (
    SELECT kind AS k, state AS st, coalesce(close_reason, '-') AS why, count(*) AS n
    FROM work_items WHERE kind IN ('replace_departed_contact', 'reply_followup_when_back', 'review_reply')
    GROUP BY 1, 2, 3) t ORDER BY 1;
