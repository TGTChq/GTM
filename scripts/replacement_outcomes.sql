-- What happened to every departure we were asked to replace.
--
-- A departure reply suppresses the address and queues `replace_departed_contact` on the
-- company x function unit. `services.replacements` hands that unit back to ordinary
-- qualification. From there it is indistinguishable from any other unit -- which is the
-- point, because a replacement must pass the same gates -- so the only honest way to
-- report on it afterwards is to follow the unit by `reopened_at` and see how far it got.
--
-- Three different things are counted, and none is inferred from another:
--
--   task_processed   -- the queued departure was decided (re-opened, or closed)
--   candidate_found  -- that unit produced an approval after it was re-opened
--   instantly_created -- that approval produced a GENUINE creation receipt in Instantly
--
-- Anything that stops short carries the precise reason it stopped.

\echo '== 1. the queue itself =='
SELECT COALESCE(w.state, '(none)') AS work_state,
       COALESCE(NULLIF(split_part(w.close_reason, ':', 1), ''), '(open)') AS outcome,
       count(*) AS n
FROM work_items w
WHERE w.kind = 'replace_departed_contact'
GROUP BY 1, 2 ORDER BY n DESC;

\echo '== 2. how far each re-opened unit got =='
WITH reopened AS (
    SELECT o.id, o.state, COALESCE(o.close_reason, '') AS close_reason, o.reopened_at
    FROM opportunities o
    WHERE o.reopened_at IS NOT NULL
      AND EXISTS (SELECT 1 FROM work_items w
                  WHERE w.kind = 'replace_departed_contact' AND w.subject_kind = 'opportunity'
                    AND w.subject_id = o.id)
), scored AS (
    SELECT r.id, r.state, r.close_reason,
           (SELECT count(*) FROM approvals a
             WHERE a.opportunity_id = r.id AND a.created_at >= r.reopened_at) AS approvals_after,
           (SELECT count(*) FROM approvals a
              JOIN delivery_outbox ob ON ob.approval_id = a.id AND ob.channel = 'instantly'
              JOIN delivery_receipts rc ON rc.outbox_id = ob.id
             WHERE a.opportunity_id = r.id AND a.created_at >= r.reopened_at
               AND rc.channel = 'instantly' AND rc.receipt_kind = 'created') AS created_after
    FROM reopened r
)
SELECT CASE WHEN created_after > 0 THEN '3_instantly_created'
            WHEN approvals_after > 0 THEN '2_candidate_found_not_yet_created'
            ELSE '1_reopened_no_candidate_yet' END AS how_far,
       state AS unit_state,
       COALESCE(NULLIF(close_reason, ''), '(none)') AS unit_close_reason,
       count(*) AS units
FROM scored GROUP BY 1, 2, 3 ORDER BY 1, units DESC;

\echo '== 3. for the ones that never reached delivery, the precise reason =='
WITH reopened AS (
    SELECT o.id, o.reopened_at FROM opportunities o
    WHERE o.reopened_at IS NOT NULL
      AND EXISTS (SELECT 1 FROM work_items w
                  WHERE w.kind = 'replace_departed_contact' AND w.subject_kind = 'opportunity'
                    AND w.subject_id = o.id)
)
SELECT COALESCE(NULLIF(ob.state, ''), '(no outbox row)') AS outbox_state,
       COALESCE(NULLIF(ob.last_error, ''), '(none)') AS last_error,
       count(*) AS n
FROM reopened r
JOIN approvals a ON a.opportunity_id = r.id AND a.created_at >= r.reopened_at
LEFT JOIN delivery_outbox ob ON ob.approval_id = a.id AND ob.channel = 'instantly'
WHERE NOT EXISTS (SELECT 1 FROM delivery_receipts rc
                  WHERE rc.outbox_id = ob.id AND rc.receipt_kind = 'created')
GROUP BY 1, 2 ORDER BY n DESC;

\echo '== 4. the departed addresses stay suppressed =='
SELECT count(*) AS departed_suppressions
FROM suppressions WHERE kind = 'person_email' AND reason IN ('no_longer_here', 'departed');
