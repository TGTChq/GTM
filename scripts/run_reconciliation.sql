-- Reconcile the most recent scheduled run, keeping every population apart.
--
-- The point of the separations: backlog delivered is not production, an approval is not
-- a creation, an Airtable row is only legitimate behind a genuine Instantly creation,
-- and a Control destination would be a routing defect rather than a number.
--
--   psql ... -X -A -t -f scripts/run_reconciliation.sql
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
),
ended AS (
    SELECT l.run_id, min(l.created_at) AS ended_at
    FROM run_log l JOIN last_run r ON r.run_id = l.run_id
    WHERE l.stage = 'daily' AND l.event IN ('end', 'refused') GROUP BY l.run_id
)
SELECT 'run|' || r.run_id || '|started=' || to_char(r.started_at, 'MM-DD HH24:MI:SS')
    || '|ended=' || coalesce(to_char(e.ended_at, 'MM-DD HH24:MI:SS'), 'STILL RUNNING')
FROM last_run r LEFT JOIN ended e ON e.run_id = r.run_id;

-- 1. approvals this run produced, and how many were blocked by compliance
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'approved_this_run|' || count(*) FILTER (WHERE a.state <> 'revoked')
    || '|compliance_blocked=' || count(*) FILTER (WHERE a.state <> 'revoked' AND a.outreach_eligible IS NOT TRUE)
    || '|revoked=' || count(*) FILTER (WHERE a.state = 'revoked')
FROM approvals a, last_run r WHERE a.run_id = r.run_id;

-- 2. Instantly receipts written during the run, split by whether the approval behind
--    them belongs to this run (production) or an earlier one (backlog drained)
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'instantly|' || kind || '|' || origin || '|' || n FROM (
    SELECT rc.receipt_kind AS kind,
           CASE WHEN a.run_id = r.run_id THEN 'this_run' ELSE 'backlog' END AS origin,
           count(*) AS n
    FROM delivery_receipts rc
    JOIN delivery_outbox o ON o.id = rc.outbox_id
    JOIN approvals a ON a.id = o.approval_id, last_run r
    WHERE rc.channel = 'instantly' AND rc.received_at >= r.started_at
    GROUP BY rc.receipt_kind, CASE WHEN a.run_id = r.run_id THEN 'this_run' ELSE 'backlog' END) t
ORDER BY kind, origin;

-- 3. Airtable receipts in the same window, same split
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'airtable|' || kind || '|' || origin || '|' || n FROM (
    SELECT rc.receipt_kind AS kind,
           CASE WHEN a.run_id = r.run_id THEN 'this_run' ELSE 'backlog' END AS origin,
           count(*) AS n
    FROM delivery_receipts rc
    JOIN delivery_outbox o ON o.id = rc.outbox_id
    JOIN approvals a ON a.id = o.approval_id, last_run r
    WHERE rc.channel = 'airtable' AND rc.received_at >= r.started_at
    GROUP BY rc.receipt_kind, CASE WHEN a.run_id = r.run_id THEN 'this_run' ELSE 'backlog' END) t
ORDER BY kind, origin;

-- 4. the invariant: an Airtable row only behind a genuine Instantly creation
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'airtable_without_creation|' || count(*)
FROM delivery_receipts ar
JOIN delivery_outbox ao ON ao.id = ar.outbox_id, last_run r
WHERE ar.channel = 'airtable' AND ar.receipt_kind IN ('created', 'reconciled') AND ar.received_at >= r.started_at
  AND NOT EXISTS (
    SELECT 1 FROM delivery_outbox io JOIN delivery_receipts ir ON ir.outbox_id = io.id
    WHERE io.approval_id = ao.approval_id AND io.channel = 'instantly'
      AND ir.channel = 'instantly' AND ir.receipt_kind = 'created');

-- 5. every destination the run created into: a Control id here would be a routing defect
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'destination|' || c || '|' || n FROM (
    SELECT coalesce(rc.external_campaign, '(none)') AS c, count(*) AS n
    FROM delivery_receipts rc, last_run r
    WHERE rc.channel = 'instantly' AND rc.receipt_kind = 'created' AND rc.received_at >= r.started_at
    GROUP BY coalesce(rc.external_campaign, '(none)')) t ORDER BY c;

-- 6. distinct people, so a creation count can never be inflated by two rows for one person
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'distinct_people_created|' || count(DISTINCT p.email)
FROM delivery_receipts rc
JOIN delivery_outbox o ON o.id = rc.outbox_id
JOIN approvals a ON a.id = o.approval_id
JOIN people p ON p.id = a.person_id, last_run r
WHERE rc.channel = 'instantly' AND rc.receipt_kind = 'created' AND rc.received_at >= r.started_at;

-- 7. provider spend for the run's budget
WITH last_run AS (
    SELECT run_id, created_at AS started_at
    FROM run_log WHERE stage = 'daily' AND event = 'start'
    ORDER BY created_at DESC LIMIT 1
)
SELECT 'spend|' || b || '|' || p || '|' || st || '|n=' || n || '|credits=' || c
    || '|in_tok=' || coalesce(it, 0) || '|out_tok=' || coalesce(ot, 0) FROM (
    SELECT sr.budget_id AS b, sr.provider AS p, sr.status AS st, count(*) AS n,
           round(coalesce(sum(sr.estimated_credits), 0)::numeric, 1) AS c,
           sum(sr.input_tokens_used) AS it, sum(sr.output_tokens_used) AS ot
    FROM spend_reservations sr, last_run r
    WHERE sr.created_at >= r.started_at
    GROUP BY sr.budget_id, sr.provider, sr.status) t ORDER BY p, st;

-- 8. what is still waiting, and why
SELECT 'waiting|' || ch || '|' || st || '|' || note || '|' || n FROM (
    SELECT channel AS ch, state AS st, coalesce(left(last_error, 38), '-') AS note, count(*) AS n
    FROM delivery_outbox WHERE state <> 'delivered'
    GROUP BY channel, state, coalesce(left(last_error, 38), '-')) t ORDER BY ch, st;

-- 9. the reply-driven queues after the run
SELECT 'work|' || k || '|' || st || '|' || why || '|' || n FROM (
    SELECT kind AS k, state AS st, coalesce(close_reason, coalesce(waiting_on, '-')) AS why, count(*) AS n
    FROM work_items
    WHERE kind IN ('replace_departed_contact', 'reply_followup_when_back', 'review_reply')
    GROUP BY kind, state, coalesce(close_reason, coalesce(waiting_on, '-'))) t ORDER BY k, st;
