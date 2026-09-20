"""Daily 24h acquisition canary (FANTASTIC_DAILY_24H_CANARY). SIMULATED Fantastic only.

Pure tests: no database, no network. The canary reuses the production request builder,
changes only the window (time_frame=24h, no date_created bounds) and the pagination
(one in-run traversal from offset 0 until a short page), and never writes production state.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tgtc_core.domain.acquisition_query import DISCOVERY_PROFILE, PRIORITY_PROFILE
from tgtc_core.providers.fantastic import FantasticClient
from tgtc_core.providers.http import Response
from tgtc_core.services import daily_24h_canary as canary
from tgtc_core.services.acquisition import SOURCE_ATS, SOURCE_JOB_BOARDS, AcquisitionService
from tgtc_core.testing.fakes import make_posting_row

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
JB, ATS = "/v1/active-jb", "/v1/active-ats"

ENG = ("You will design and build APIs and microservices in Python. You will maintain CI/CD pipelines "
       "with Kubernetes and Docker. Write unit tests and take part in code reviews every week.")
CLINICAL = ("Provide direct patient care at the bedside; patient care responsibilities are required on every "
            "shift. You will work with physicians and nurses in the emergency department of the hospital.")
VAGUE = ("Join a friendly team that values curiosity and ownership. You will work on interesting problems with "
         "great people and grow with the company as it expands into new markets over the next few years.")


def row(i, *, description=ENG, headcount=120, industry="Software Development", title=None, url=None, org=None, **kw):
    org = org or f"Acme {i}"
    return make_posting_row(id=str(1000 + i), title=title or f"Software Engineer {i}", organization=org,
                            domain=f"acme{i}.com", description=description, date_created=NOW - timedelta(hours=2),
                            headcount=headcount, industry=industry, url=url or f"https://jobs.example/{1000 + i}", **kw)


class Feed24h:
    """Scriptable 24h feed: rows per endpoint, served by offset; repeats, failures, counts."""

    def __init__(self, jb=(), ats=(), repeat_at=None, fail_at=None, count=None):
        self.rows = {JB: list(jb), ATS: list(ats)}
        self.repeat_at = repeat_at          # (endpoint, offset): serve the previous page again
        self.fail_at = dict(fail_at or {})  # (endpoint, offset) -> http status
        self.count = count                  # body returned by *-count endpoints
        self.requests = []

    def request(self, method, url, *, headers=None, params=None, json_body=None, timeout=30.0):
        path = urlsplit(url).path
        p = dict(params or {})
        self.requests.append({"path": path, "params": p, "auth": bool((headers or {}).get("Authorization"))})
        if path.endswith("-count"):
            return Response(200, {}, json.dumps(self.count if self.count is not None else {"count": 0}))
        offset, limit = int(p["offset"]), int(p["limit"])
        if (path, offset) in self.fail_at:
            return Response(self.fail_at[(path, offset)], {}, json.dumps({"error": "simulated"}))
        rows = self.rows[path]
        page = rows[offset - limit:offset] if self.repeat_at == (path, offset) else rows[offset:offset + limit]
        return Response(200, {"x-api-jobs-this-request": str(len(page)), "x-api-jobs-remaining": "9000",
                              "x-api-requests-remaining": "900"}, json.dumps(page))


def client(feed):
    return FantasticClient(feed, base_url="https://fantastic.test", api_key="k-test")


def run(feed, *, partitions=((SOURCE_JOB_BOARDS, PRIORITY_PROFILE),), limit=2, max_records=1000, max_requests=100,
        registry=None, **kw):
    c = canary.Daily24hCanary(client(feed), partitions=partitions, limit=limit, max_records=max_records,
                              max_requests=max_requests, registry=registry or canary.HistoricalRegistry.empty(),
                              now=lambda: NOW, **kw)
    return c.run()


def offsets(feed, path=JB):
    return [int(r["params"]["offset"]) for r in feed.requests if r["path"] == path]


# ---------------------------------------------------------------------------- request shape

def test_sends_the_documented_24h_frame_and_no_date_created_bounds():
    assert canary.DAILY_TIME_FRAME == "24h" and canary.DAILY_TIME_FRAME in canary.DOCUMENTED_TIME_FRAMES
    endpoint, params = canary.daily_request(SOURCE_JOB_BOARDS, PRIORITY_PROFILE, offset=0, limit=250)
    assert endpoint == JB
    assert params["time_frame"] == "24h"
    assert "date_created_gte" not in params and "date_created_lt" not in params
    assert params["offset"] == 0 and params["limit"] == 250


@pytest.mark.parametrize("source,profile", [(SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_ATS, PRIORITY_PROFILE),
                                            (SOURCE_JOB_BOARDS, DISCOVERY_PROFILE), (SOURCE_ATS, DISCOVERY_PROFILE)])
def test_every_other_parameter_equals_the_production_request(source, profile):
    """Same endpoint, same filters, same query partition: only the window changes."""
    svc = AcquisitionService.__new__(AcquisitionService)
    svc.page_limit, svc.time_frame, svc.location = 100, "7d", "United States"
    svc.sources = (SOURCE_JOB_BOARDS, SOURCE_ATS)
    svc.now = lambda: NOW
    endpoint, prod = svc.request_params(source, lower=NOW - timedelta(hours=5), upper=NOW - timedelta(hours=4),
                                        offset=0, profile=profile)
    endpoint2, daily = canary.daily_request(source, profile, offset=0, limit=100)
    assert endpoint2 == endpoint
    for key in ("time_frame", "date_created_gte", "date_created_lt"):
        prod.pop(key, None)
        daily.pop(key, None)
    assert daily == prod


def test_existing_partitions_are_the_production_profiles_on_both_feeds():
    assert set(canary.EXISTING_PARTITIONS) == {(SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_ATS, PRIORITY_PROFILE),
                                               (SOURCE_JOB_BOARDS, DISCOVERY_PROFILE), (SOURCE_ATS, DISCOVERY_PROFILE)}


# ---------------------------------------------------------------------------- pagination

def test_begins_at_offset_zero_and_advances_offset_after_every_full_page():
    feed = Feed24h(jb=[row(i) for i in range(5)])
    rep = run(feed, limit=2)
    assert offsets(feed) == [0, 2, 4]
    assert all(r["params"]["time_frame"] == "24h" for r in feed.requests)
    part = rep["partitions"][0]
    assert part["stop_reason"] == "short_page" and part["completed"] is True
    assert rep["traversal_complete"] is True


def test_requests_another_page_after_an_exactly_full_last_page():
    feed = Feed24h(jb=[row(i) for i in range(4)])
    rep = run(feed, limit=2)
    assert offsets(feed) == [0, 2, 4]            # page 3 is empty: 0 < limit ends it
    assert rep["partitions"][0]["pages"][-1]["rows"] == 0
    assert rep["partitions"][0]["completed"] is True


def test_stops_after_the_first_page_smaller_than_the_limit():
    feed = Feed24h(jb=[row(i) for i in range(3)])
    run(feed, limit=5)
    assert offsets(feed) == [0]


def test_a_full_page_of_already_seen_rows_is_not_exhaustion():
    rows = [row(i) for i in range(5)]
    seen = canary.HistoricalRegistry.from_rows([(SOURCE_JOB_BOARDS, r["id"], "", "") for r in rows[:2]])
    feed = Feed24h(jb=rows)
    rep = run(feed, limit=2, registry=seen)
    assert offsets(feed) == [0, 2, 4]            # no no_new_ids stop after the all-seen first page
    first = rep["partitions"][0]["pages"][0]
    assert first["historical_previously_seen"] == 2 and first["net_new"] == 0
    assert rep["partitions"][0]["stop_reason"] == "short_page"


def test_a_repeated_page_signature_aborts_the_partition_as_incomplete():
    feed = Feed24h(jb=[row(i) for i in range(6)], repeat_at=(JB, 2))
    rep = run(feed, limit=2)
    part = rep["partitions"][0]
    assert part["stop_reason"] == "repeated_page_signature" and part["completed"] is False
    assert offsets(feed) == [0, 2]
    assert rep["traversal_complete"] is False


@pytest.mark.parametrize("status,reason", [(500, "provider_error:request_error:http_500"),
                                           (401, "provider_error:auth"), (429, "provider_error:quota")])
def test_a_provider_error_stops_the_traversal_as_incomplete(status, reason):
    feed = Feed24h(jb=[row(i) for i in range(6)], ats=[row(i) for i in range(6)], fail_at={(JB, 2): status})
    rep = run(feed, partitions=((SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_ATS, PRIORITY_PROFILE)), limit=2)
    assert rep["partitions"][0]["stop_reason"] == reason
    assert rep["traversal_complete"] is False
    assert offsets(feed, ATS) == []              # fail closed: no further partition is requested


def test_record_ceiling_is_enforced_before_the_request_and_truncates():
    feed = Feed24h(jb=[row(i) for i in range(10)])
    rep = run(feed, limit=2, max_records=5)
    assert offsets(feed) == [0, 2]               # a third full page could exceed 5 billed rows
    assert rep["records_billed"] == 4
    assert rep["partitions"][0]["stop_reason"] == "record_ceiling"
    assert rep["traversal_complete"] is False


def test_request_ceiling_counts_preflight_requests_and_truncates():
    feed = Feed24h(jb=[row(i) for i in range(10)])
    rep = run(feed, limit=2, max_requests=3, requests_already_used=1)
    assert len(feed.requests) == 2
    assert rep["partitions"][0]["stop_reason"] == "request_ceiling"
    assert rep["provider_requests"] == 3 and rep["traversal_complete"] is False


# ---------------------------------------------------------------------------- dedupe accounting

def test_every_billed_row_lands_in_exactly_one_dedupe_bucket():
    rows = [row(0), row(1), row(2)]
    dup_in_page = dict(rows[0])
    same_url = row(9, url=rows[1]["url"] + "?utm=x")               # canonical URL twin of row 1
    seen = canary.HistoricalRegistry.from_rows([(SOURCE_JOB_BOARDS, rows[2]["id"], "", "")])
    feed = Feed24h(jb=[rows[0], dup_in_page, rows[1], same_url, rows[2]],
                   ats=[row(20)])
    rep = run(feed, partitions=((SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_JOB_BOARDS, DISCOVERY_PROFILE),
                                (SOURCE_ATS, PRIORITY_PROFILE)), limit=10, registry=seen)
    t = rep["totals"]
    assert t["rows_returned"] == 5 + 5 + 1
    assert t["within_page_duplicates"] == 1
    assert t["cross_query_duplicates"] == 5                     # discovery returned the priority rows again
    assert t["historical_previously_seen"] == 1
    assert t["canonical_url_duplicates"] == 1
    assert t["net_new_unique_jobs"] == 3                        # rows 0, 1 and the ATS row
    buckets = ("within_page_duplicates", "cross_page_duplicates", "cross_query_duplicates",
               "historical_previously_seen", "canonical_url_duplicates", "canonical_fingerprint_duplicates",
               "net_new_unique_jobs")
    assert sum(t[b] for b in buckets) == t["rows_returned"]


def test_canonical_fingerprint_catches_the_same_job_under_two_ids():
    a = row(1)
    b = dict(a, id="5555", url="https://other.example/5555")
    feed = Feed24h(jb=[a], ats=[b])
    rep = run(feed, partitions=((SOURCE_JOB_BOARDS, PRIORITY_PROFILE), (SOURCE_ATS, PRIORITY_PROFILE)), limit=5)
    assert rep["totals"]["canonical_fingerprint_duplicates"] == 1
    assert rep["totals"]["net_new_unique_jobs"] == 1


def test_ats_duplicate_flag_is_counted():
    feed = Feed24h(jb=[row(1, ats_duplicate=True), row(2)])
    rep = run(feed, limit=5)
    assert rep["totals"]["ats_duplicate_true"] == 1


# ---------------------------------------------------------------------------- deterministic qualification

def test_deterministic_qualification_never_uses_a_model_and_separates_ambiguity():
    feed = Feed24h(jb=[row(1), row(2, description=CLINICAL), row(3, description=VAGUE), row(4, headcount=None),
                       row(5, headcount=5000), row(6, industry="Hospitals and Health Care")])
    rep = run(feed, limit=10)
    q = rep["qualification"]
    assert q["qualified_pre_contact_jobs"] == 1
    assert q["by_outcome"]["rejected:deliverability:physical_facility"] == 1
    assert q["by_outcome"]["ambiguous:needs_semantic_classifier"] == 1
    assert q["by_outcome"]["ambiguous:company_size_unknown"] == 1
    assert q["by_outcome"]["rejected:employer_too_large"] == 1
    assert q["by_outcome"]["rejected:employer_excluded_industry:hospitals and health care"] == 1
    assert q["reviewed"] == 6 and q["model_calls"] == 0


# ---------------------------------------------------------------------------- state isolation

def test_the_canary_module_cannot_reach_production_state_or_downstream_stages():
    tree = ast.parse((ROOT / "tgtc_core/services/daily_24h_canary.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    banned = ("db", "work_queue", "psycopg", "delivery", "suppression", "opportunity", "approval", "apollo",
              "airtable", "instantly", "inference", "spend_budget", "runner", "identity_service",
              "classification_service", "lifecycle")
    offenders = sorted(m for m in imported if any(part in banned for part in m.replace(".", " ").split()))
    assert offenders == []


def test_registry_file_is_read_only_and_artifacts_stay_in_the_state_dir(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"postings": [[SOURCE_JOB_BOARDS, "1000", "", ""]]}), encoding="utf-8")
    before = hashlib.sha256(reg.read_bytes()).hexdigest()
    registry = canary.HistoricalRegistry.from_file(reg)
    feed = Feed24h(jb=[row(i) for i in range(3)], fail_at={(JB, 2): 500})
    rep = run(feed, limit=2, registry=registry)
    state = tmp_path / "state"
    canary.write_artifacts(rep, state)
    assert hashlib.sha256(reg.read_bytes()).hexdigest() == before
    assert rep["production_state_written"] is False and rep["traversal_complete"] is False
    written = {p.name for p in state.iterdir()}
    assert {"report.json", "pages.jsonl", "requests_redacted.jsonl"} <= written
    with pytest.raises(FileExistsError):
        canary.write_artifacts(rep, state)       # never overwrites an earlier canary's evidence


def test_requests_log_never_contains_the_key(tmp_path):
    feed = Feed24h(jb=[row(1)])
    rep = run(feed, limit=5)
    canary.write_artifacts(rep, tmp_path / "s")
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (tmp_path / "s").iterdir()
                   if p.suffix != ".gz")
    assert "k-test" not in text and "Authorization" not in text


# ---------------------------------------------------------------------------- flag / production unchanged

def test_flag_is_off_by_default_and_only_1_enables_it():
    assert canary.FLAG_ENV == "FANTASTIC_DAILY_24H_CANARY"
    assert canary.flag_enabled({}) is False
    assert canary.flag_enabled({"FANTASTIC_DAILY_24H_CANARY": "0"}) is False
    assert canary.flag_enabled({"FANTASTIC_DAILY_24H_CANARY": "true"}) is False
    assert canary.flag_enabled({"FANTASTIC_DAILY_24H_CANARY": "1"}) is True


def test_cli_refuses_without_the_flag(monkeypatch, tmp_path):
    from tgtc_core.__main__ import main
    monkeypatch.delenv("FANTASTIC_DAILY_24H_CANARY", raising=False)
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["canary-24h", "--state-dir", str(tmp_path / "s"), "--dry-run"])
    assert "FANTASTIC_DAILY_24H_CANARY" in str(exc.value)


def test_dry_run_renders_the_plan_without_any_network(monkeypatch, tmp_path, capsys):
    from tgtc_core.__main__ import main
    import tgtc_core.providers.http as http

    def boom(*a, **k):
        raise AssertionError("network used during dry run")

    monkeypatch.setattr(http.RequestsTransport, "request", boom)
    monkeypatch.setenv("FANTASTIC_DAILY_24H_CANARY", "1")
    monkeypatch.delenv("TGTC_ACCEPTANCE_MODE", raising=False)
    assert main(["canary-24h", "--state-dir", str(tmp_path / "s"), "--dry-run", "--limit", "250",
                 "--max-records", "5000", "--max-requests", "25"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [p["params"]["offset"] for p in plan["partitions"][0]["planned_pages"][:3]] == [0, 250, 500]
    assert plan["partitions"][0]["params"]["time_frame"] == "24h"
    assert "Authorization" not in json.dumps(plan)


def test_production_acquisition_is_unchanged_when_the_flag_is_off(monkeypatch):
    """The deployed mode still sends time_frame=7d with explicit date_created bounds."""
    monkeypatch.delenv("FANTASTIC_DAILY_24H_CANARY", raising=False)
    svc = AcquisitionService.__new__(AcquisitionService)
    svc.page_limit, svc.time_frame, svc.location = 100, "7d", "United States"
    svc.sources = (SOURCE_JOB_BOARDS, SOURCE_ATS)
    svc.now = lambda: NOW
    _, params = svc.request_params(SOURCE_JOB_BOARDS, lower=NOW - timedelta(hours=5), upper=NOW - timedelta(hours=4),
                                   offset=0, profile=PRIORITY_PROFILE)
    assert params["time_frame"] == "7d"
    assert params["date_created_gte"] == "2026-09-19T07:00:00Z" and params["date_created_lt"] == "2026-09-19T08:00:00Z"


def test_count_preflight_parses_documented_body_shapes():
    for body in (42, {"count": 42}, {"total": 42}):
        feed = Feed24h(count=body)
        total, _ = canary.count_total(feed, base_url="https://fantastic.test", api_key="k-test",
                                      endpoint=JB, params={"time_frame": "24h", "location": "United States"})
        assert total == 42
    probe = feed.requests[-1]
    assert probe["path"] == "/v1/active-jb-count" and "time_frame" not in probe["params"]


# ---------------------------------------------------------------------------
# Final whole-branch review, CANARY: the "frozen instrument" ruling was wrong.
# qualify_row calls classify_posting -> extract_job_facts, so tasks 1-4 and 6
# ALREADY changed this canary's output (the committed fixture diff proves it).
# What it did NOT have was the company-size half: three-state size everywhere
# else, a single-field org_linkedin_headcount read here -- one canary running
# two different policies. It now asks the SAME resolve_company_size /
# size_reject_reason predicate the live gates (services/opportunity._size_gate,
# domain/approval.build_approved_lead) ask.
# ---------------------------------------------------------------------------

def _qualify(**over):
    return canary.qualify_row(row(1, **over), now=NOW)["outcome"]


def test_the_canary_size_verdict_matches_the_live_size_predicate():
    from tgtc_core.domain.facts import resolve_company_size
    from tgtc_core.policy.requirements import rule

    cases = [(120, "51-200 employees"), (120, None), (5000, "1,001-5,000 employees"),
             (5000, "51-200 employees"), (None, "51-200 employees"), (None, None), (12, "1-10 employees")]
    for headcount, band in cases:
        outcome = _qualify(headcount=headcount, org_linkedin_size=band)
        state, _excerpt, _effective = resolve_company_size(
            headcount, band, description=ENG,
            min_employees=int(rule("min_employees")), max_employees=int(rule("max_employees")))
        expected_prefix = {"in_range": "qualified_pre_contact", "out_of_range": "rejected:employer_too",
                           "firmographic_conflict": "ambiguous:firmographic_conflict",
                           "unknown_firmographics": "ambiguous:company_size_unknown"}[state]
        assert outcome.startswith(expected_prefix), (headcount, band, state, outcome)


def test_the_canary_never_rejects_a_firmographic_conflict():
    """headcount 5000 alone reads employer_too_large; the employer's own declared
    band reads clearly in range. Decision 2: never discard a potentially eligible
    company merely because two sources conflict -- so it is a review bucket."""
    assert _qualify(headcount=5000, org_linkedin_size="51-200 employees") == "ambiguous:firmographic_conflict"


def test_the_canary_reads_the_declared_band_when_no_headcount_is_present():
    assert _qualify(headcount=None, org_linkedin_size="51-200 employees") == "qualified_pre_contact"
    assert _qualify(headcount=None, org_linkedin_size=None) == "ambiguous:company_size_unknown"


def test_the_canary_still_rejects_an_employer_every_source_agrees_is_out_of_range():
    assert _qualify(headcount=5000, org_linkedin_size="1,001-5,000 employees") == "rejected:employer_too_large"
    assert _qualify(headcount=12, org_linkedin_size="1-10 employees") == "rejected:employer_too_small"


# ---------------------------------------------------------------------------
# Scoped re-review, IMPORTANT 1: after the size predicate was shared, the
# canary's headline stopped being an upper bound on approvals -- and it is the
# number a capacity decision gets read off. qualify_row maps
# firmographic_conflict and unknown_firmographics to `ambiguous:` buckets, and
# _qualify counts only `qualified_pre_contact`; but the LIVE gate PROCEEDS on
# both (services/opportunity._size_gate closes only out_of_range, and
# build_approved_lead refuses unknown only when rule("require_employee_count"),
# which is False). So the strict count is a LOWER bound on the live proceed set,
# not an upper bound on approvals. Both numbers are reported, separately.
# ---------------------------------------------------------------------------

def test_the_canary_reports_the_live_proceed_set_beside_the_strict_qualified_count():
    feed = Feed24h(jb=[
        row(1),                                                            # in_range -> qualified
        row(2, headcount=5000, org_linkedin_size="51-200 employees"),      # conflict -> proceeds live
        row(3, headcount=None, org_linkedin_size=None),                    # unknown  -> proceeds live
        row(4, headcount=5000, org_linkedin_size="1,001-5,000 employees"), # out_of_range -> rejected
    ])
    q = run(feed, limit=10)["qualification"]
    assert q["qualified_pre_contact_jobs"] == 1
    assert q["size_review_jobs"] == 2
    assert q["proceeds_to_contact_jobs"] == 3, "the live gate proceeds on a conflict and on an unknown"
    assert q["proceeds_to_contact_opportunities"] == 3
    assert q["rejected"] == 1
    # the two figures stay separate: the strict count is not folded into the proceed set
    assert q["qualified_pre_contact_jobs"] != q["proceeds_to_contact_jobs"]


def test_the_canary_definition_names_which_figure_bounds_approvals():
    """The docstring said `qualified_pre_contact` is "an upper bound on
    approvals". It no longer is, and post-branch runs are not comparable to
    pre-branch ones -- neither figure may be readable as the other."""
    q = run(Feed24h(jb=[row(1)]), limit=10)["qualification"]
    definition = q["definition"]
    assert "proceeds_to_contact_jobs" in definition and "qualified_pre_contact_jobs" in definition
    assert "upper bound" in definition.lower() and "lower bound" in definition.lower()
    doc = " ".join((canary.qualify_row.__doc__ or "").split())
    assert "is NOT an upper bound on approvals" in doc, "qualify_row still claims the strict figure bounds approvals"
    assert "proceeds_to_contact_jobs" in doc, "the corrected docstring must name the figure that does bound them"
