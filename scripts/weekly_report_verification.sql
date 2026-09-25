-- Did the Friday report actually publish, once, with a CSV that reconciles?
--
-- A report is only real if five separate things line up, and none of them may be
-- inferred from another: the run exists and carries its four figures; the CSV exists
-- with a row count and a checksum; that row count equals the "added to Instantly"
-- figure in the summary; a delivery was accepted by Slack with a file id and a
-- permalink; and there is EXACTLY ONE final delivery for that report.

\echo '== 1. the report run =='
SELECT report_id, kind, status,
       window_start::timestamp(0) AS window_start,
       window_end::timestamp(0) AS window_end,
       data_cutoff::timestamp(0) AS data_cutoff,
       generated_at::timestamp(0) AS generated_at,
       coalesce(delivery_target, '(none)') AS delivery_target,
       coalesce(delivered_at::timestamp(0)::text, '(not delivered)') AS delivered_at
FROM report_runs ORDER BY generated_at DESC LIMIT 3;

\echo '== 2. the four figures it published =='
SELECT report_id, key, value FROM (
  SELECT report_id, k AS key, payload_json #>> ARRAY[k] AS value
  FROM report_runs, LATERAL jsonb_object_keys(payload_json) k
  WHERE generated_at = (SELECT max(generated_at) FROM report_runs)
) t WHERE jsonb_typeof(to_jsonb(value)) IS NOT NULL ORDER BY key LIMIT 40;

\echo '== 3. every delivery recorded for it, and how the destination was established =='
SELECT d.report_id, d.channel, d.kind, d.delivered_at::timestamp(0) AS at,
       d.destination_basis, d.attempts,
       left(d.receipt::text, 400) AS receipt
FROM report_deliveries d
WHERE d.report_id = (SELECT report_id FROM report_runs ORDER BY generated_at DESC LIMIT 1);

\echo '== 4. exactly one final delivery per report -- more than one is a double publish =='
SELECT report_id, channel, n FROM (
  SELECT report_id, channel, count(*) AS n FROM report_deliveries WHERE kind = 'final'
  GROUP BY report_id, channel) t
WHERE n <> 1 ORDER BY report_id;

\echo '== 5. the CSV export: rows, checksum, and where it went (never the bytes) =='
SELECT report_id, row_count, sha256,
       jsonb_array_length(columns_json) AS columns,
       length(csv_gzip) AS gzip_bytes,
       generated_at::timestamp(0) AS generated_at,
       coalesce(published_url, '(not published)') AS published_url,
       coalesce(published_at::timestamp(0)::text, '-') AS published_at,
       coalesce(published_to::text, '-') AS published_to
FROM report_lead_exports ORDER BY generated_at DESC LIMIT 3;

\echo '== 6. does the CSV row count equal what the summary claims was added to Instantly? =='
SELECT e.report_id, e.row_count AS csv_rows,
       r.payload_json #>> '{added_to_instantly}' AS summary_added_to_instantly,
       (e.row_count::text IS NOT DISTINCT FROM r.payload_json #>> '{added_to_instantly}') AS they_agree
FROM report_lead_exports e JOIN report_runs r ON r.report_id = e.report_id
ORDER BY e.generated_at DESC LIMIT 3;
