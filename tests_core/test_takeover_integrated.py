"""Takeover regressions against disposable PostgreSQL; never a live provider.

These tests must run in the core CI/local PostgreSQL gate, not be replaced by mocks.
"""
from datetime import timedelta
from pathlib import Path

import pytest

from tgtc_core.db import apply_schema, work_queue
from tgtc_core.domain.inference import InferenceResponse, UNAVAILABLE_ANSWER
from tgtc_core.services import provider_state
from tgtc_core.services.acquisition import upsert_posting
from tgtc_core.services.classification_service import classify_one, reopen_for_inference
from tgtc_core.services.identity_service import resolve_posting_identity
from tgtc_core.testing.fakes import FakeAirtable, FakeFantastic
from tests_core.helpers import acquisition, delivery_service, make_fresh_partition, opportunity_service, rows_in_window, sql1
from tests_core.seed import apollo_for, good_buyer, seed_opportunity
from tests_core.test_review_r03_inference_recovery import _seed_vague


def test_content_refusal_is_not_reopened_each_cycle_for_the_same_model(conn, clock):
    pid = _seed_vague(conn, clock)
    class Refusal:
        model_version = 'model/1'
        def classify(self, request):
            return InferenceResponse(available=False, unavailable_kind=UNAVAILABLE_ANSWER,
                                     unavailable_reason='model_refusal', model_version=self.model_version)
    assert classify_one(conn, pid, inference=Refusal(), now=clock()).outcome == 'closed'
    assert reopen_for_inference(conn, model_version='model/1', now=clock()) == 0
    assert reopen_for_inference(conn, model_version='model/2', now=clock()) == 1


def test_same_posting_replay_cannot_consume_another_evidence_epoch(conn, clock):
    pid, _, oid = seed_opportunity(conn, clock)
    fake = apollo_for('acme.com', 'Acme', people=[])
    assert opportunity_service(conn, fake, clock).process(oid).outcome == 'closed'
    for _ in range(5):
        classify_one(conn, pid, inference=None, now=clock())
    assert sql1(conn, 'SELECT evidence_epoch FROM opportunities WHERE id = %s', (oid,)) == 1
    assert sql1(conn, 'SELECT state FROM opportunities WHERE id = %s', (oid,)) == 'closed'
    seed_opportunity(conn, clock, job_id='genuinely-new-posting')
    assert sql1(conn, 'SELECT evidence_epoch FROM opportunities WHERE id = %s', (oid,)) == 2


def test_free_empty_search_does_not_consume_the_paid_recovery_probe(conn, clock):
    _, _, first = seed_opportunity(conn, clock)
    _, _, second = seed_opportunity(conn, clock, domain='beta.com', org_name='Beta', job_id='job-beta')
    provider_state.record_refusal(conn, 'apollo', 'credits_exhausted', now=clock())
    clock.advance(hours=6, seconds=1)
    assert opportunity_service(conn, apollo_for('acme.com', 'Acme', people=[]), clock).process(first).outcome == 'closed'
    fake = apollo_for('beta.com', 'Beta', people=[good_buyer('beta.com', 'Beta', id='beta-buyer')])
    assert opportunity_service(conn, fake, clock).process(second).outcome == 'approved'
    assert fake.served_paid == 1


def test_fantastic_refusal_blocks_new_partitions_as_well_as_stalled_ones(conn, clock):
    provider_state.record_refusal(conn, 'fantastic', 'quota', now=clock())
    fake = FakeFantastic(rows=rows_in_window(clock, 2))
    svc = acquisition(conn, fake, clock)
    pid = make_fresh_partition(conn, clock)
    assert svc.run_partition(pid).stop_reason == 'provider_refusing'
    assert fake.requests == []
    clock.advance(hours=6, seconds=1)
    assert svc.run_partition(pid).stop_reason == 'complete'
    assert len(fake.requests) == 1


def test_expired_delivery_lease_cannot_send_even_without_a_replacement_worker(conn, clock):
    _, _, oid = seed_opportunity(conn, clock)
    assert opportunity_service(conn, apollo_for('acme.com', 'Acme'), clock).process(oid).outcome == 'approved'
    fake = FakeAirtable()
    svc = delivery_service(conn, fake, None, clock)
    item = svc.claim('airtable')[0]
    clock.advance(seconds=301)
    assert svc._receipt(item, 'created', external_id='stale') is False
    assert svc._set(item, 'delivered') is False
    assert svc.process(item).outcome == 'lease_lost'
    assert fake.requests == []
    assert sql1(conn, 'SELECT count(*) FROM delivery_receipts') == 0


def test_stale_work_lease_cannot_change_identity_business_state(conn, conn2, clock):
    pid, eid, _ = seed_opportunity(conn, clock)
    work_queue.enqueue(conn, kind='resolve_identity', subject_kind='posting', subject_id=pid, available_at=clock())
    conn.commit()
    old = work_queue.claim(conn, kind='resolve_identity', lane='fresh', lease_seconds=60, now=clock())
    conn.commit()
    clock.advance(seconds=61)
    fresh = work_queue.claim(conn2, kind='resolve_identity', lane='fresh', lease_seconds=60, now=clock())
    conn2.commit()
    with pytest.raises(work_queue.LeaseLost):
        resolve_posting_identity(conn, pid, now=clock(), work_item=old)
    assert sql1(conn, 'SELECT state FROM postings WHERE id = %s', (pid,)) == 'classified'
    assert sql1(conn, 'SELECT employer_id FROM postings WHERE id = %s', (pid,)) == eid
    assert resolve_posting_identity(conn2, pid, now=clock(), work_item=fresh).outcome == 'resolved'


def test_model_answer_for_an_old_snapshot_cannot_overwrite_new_facts(conn, conn2, clock):
    pid = _seed_vague(conn, clock)
    class ChangingPort:
        model_version = 'model/1'
        def classify(self, request):
            with conn2.cursor() as cur:
                cur.execute("UPDATE postings SET content_hash = 'new-facts', countries = ARRAY['GB'] WHERE id = %s", (pid,))
            conn2.commit()
            return InferenceResponse(available=True, model_version=self.model_version,
                                     incompatible_reasons=['physical work'])
    with pytest.raises(work_queue.EvidenceChanged):
        classify_one(conn, pid, inference=ChangingPort(), now=clock())
    assert sql1(conn, 'SELECT state FROM postings WHERE id = %s', (pid,)) == 'identity_resolved'
    assert sql1(conn, 'SELECT count(*) FROM classifications WHERE posting_id = %s', (pid,)) == 0


def test_v1_migration_preserves_inventory_and_cursor(conn, clock):
    # conn is an isolated test schema created by the fixture, never production.
    schema = Path(__file__).with_name('fixtures').joinpath('schema_v1.sql').read_text()
    with conn.cursor() as cur:
        cur.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        cur.execute(schema)
        cur.execute("INSERT INTO source_partitions (source, lane, window_start, window_end, next_offset) "
                    "VALUES ('fantastic:active-jb', 'fresh', %s, %s, 100)", (clock()-timedelta(hours=2), clock()))
        cur.execute("INSERT INTO postings (source, provider_job_id, content_hash, commercial_age_anchor) "
                    "VALUES ('fantastic:active-jb', 'saved-job', 'saved-hash', %s)", (clock(),))
    conn.commit()
    apply_schema(conn)
    apply_schema(conn)
    assert sql1(conn, 'SELECT next_offset FROM source_partitions') == 100
    assert sql1(conn, 'SELECT provider_job_id FROM postings') == 'saved-job'
    assert sql1(conn, 'SELECT max(version) FROM schema_migrations') == 3


def test_late_page_keeps_a_receipt_without_overwriting_the_new_owner_data(conn, conn2, clock):
    rows_old = rows_in_window(clock, 1)
    rows_new = [dict(rows_old[0], description_text='New authoritative description from the second worker.')]
    fake_b = FakeFantastic(rows=rows_new)
    b = acquisition(conn2, fake_b, clock, lease_seconds=60)
    pid = make_fresh_partition(conn, clock)
    class LatePage(FakeFantastic):
        def request(self, method, url, **kw):
            response = super().request(method, url, **kw)
            clock.advance(seconds=61)
            assert b.run_partition(pid).stop_reason == 'complete'
            return response
    a = acquisition(conn, LatePage(rows=rows_old), clock, lease_seconds=60)
    run = a.run_partition(pid)
    assert run.stop_reason == 'lease_lost' and run.new_postings == 0 and run.modified_postings == 0
    assert sql1(conn, 'SELECT description_text FROM postings') == rows_new[0]['description_text']
    assert sql1(conn, 'SELECT count(*) FROM page_receipts WHERE fenced') == 1
    assert sql1(conn, 'SELECT count(*) FROM posting_versions') == 1
