"""Read-only, reconciled attribution. First receipt wins, never double-count a job."""


def acquisition_profiles(cur) -> dict:
    cur.execute("""
        SELECT query_profile, count(*) AS partitions,
               count(*) FILTER (WHERE complete_coverage) AS completed_query_windows,
               count(*) FILTER (WHERE NOT complete_coverage) AS incomplete_query_windows,
               min(window_start) FILTER (WHERE NOT complete_coverage) AS oldest_incomplete_window
        FROM source_partitions GROUP BY query_profile ORDER BY query_profile
    """)
    profiles = {}
    for row in cur.fetchall():
        data = dict(row)
        key = data.pop("query_profile")
        if data["oldest_incomplete_window"]:
            data["oldest_incomplete_window"] = data["oldest_incomplete_window"].isoformat()
        profiles[key] = dict(data, coverage_scope="saved_query_only_not_whole_market",
                             requests=0, job_credits_estimate=0, confirmed_job_credits=None,
                             unique_postings_first_seen=0, currently_compatible_postings=0,
                             approved_contacts=0, compatible_per_estimated_credit=None)
    cur.execute("""
        SELECT p.query_profile, count(DISTINCT a.id) AS requests,
               COALESCE(sum(c.estimated_credits), 0) AS job_credits_estimate,
               sum(c.confirmed_credits) AS confirmed_job_credits
        FROM request_attempts a JOIN source_partitions p ON p.id = a.partition_id
        LEFT JOIN credit_events c ON c.attempt_id = a.id AND c.provider = 'fantastic'
        WHERE a.provider = 'fantastic' GROUP BY p.query_profile
    """)
    for row in cur.fetchall():
        data = dict(row)
        key = data.pop("query_profile")
        profiles[key].update({k: float(v) if v is not None else None for k, v in data.items()})
    cur.execute("""
        WITH first_receipt AS (
            SELECT DISTINCT ON (p.id) p.id AS posting_id, s.query_profile
            FROM page_receipts r JOIN source_partitions s ON s.id = r.partition_id
            CROSS JOIN LATERAL unnest(r.row_ids) AS rid(provider_id)
            JOIN postings p ON p.source = s.source AND p.provider_job_id = rid.provider_id
            WHERE NOT r.fenced ORDER BY p.id, r.id
        ), latest AS (
            SELECT DISTINCT ON (posting_id) posting_id, excluded, compatible_functions
            FROM classifications ORDER BY posting_id, created_at DESC, id DESC
        )
        SELECT f.query_profile, count(DISTINCT f.posting_id) AS unique_postings_first_seen,
               count(DISTINCT f.posting_id) FILTER (
                   WHERE p.state = 'classified' AND p.duplicate_of_posting_id IS NULL
                   AND NOT l.excluded AND cardinality(l.compatible_functions) > 0
               ) AS currently_compatible_postings,
               count(DISTINCT a.id) AS approved_contacts
        FROM first_receipt f JOIN postings p ON p.id = f.posting_id
        LEFT JOIN latest l ON l.posting_id = p.id
        LEFT JOIN approvals a ON a.lead_json->>'posting_id' = p.id::text AND a.state <> 'revoked'
        GROUP BY f.query_profile
    """)
    for row in cur.fetchall():
        data = dict(row)
        key = data.pop("query_profile")
        profiles[key].update(data)
    for data in profiles.values():
        estimated = data["job_credits_estimate"]
        if estimated:
            data["compatible_per_estimated_credit"] = data["currently_compatible_postings"] / estimated
    return profiles
