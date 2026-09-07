"""Maintenance runs on the existing schedule, and cannot become a pipeline run.

The production volume is only reachable from inside a scheduled container, and the
only scheduled container ran the pipeline -- which buys jobs. `MAINTENANCE_ONLY`
lets the EXISTING cron carry the recovery/reporting pass instead, with no change to
the schedule and no change to the start command.

The two properties that make that safe:

  * it is REFUSED while acquisition is armed, so it can never be switched on in a
    way that hides a live run or is confused for one;
  * it returns before any lane runner, enrichment engine or delivery manager is
    constructed, so no acquisition, enrichment or delivery code is reachable --
    structurally, not by convention.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

import config
import run_maintenance
import run_orchestrator


class MaintenanceOnlyIsRefusedWhileAcquisitionIsArmed(unittest.TestCase):
    def test_it_will_not_run_with_the_paid_source_enabled(self):
        with mock.patch.multiple(config, MAINTENANCE_ONLY=True,
                                 FANTASTIC_JOBS_ENABLED=True):
            import run_maintenance
            with self.assertRaises(SystemExit) as caught:
                run_maintenance._refuse_if_acquisition_is_live()
            self.assertEqual(caught.exception.code, 2)

    def test_the_flag_is_off_by_default(self):
        self.assertFalse(config.MAINTENANCE_ONLY)


class MaintenanceOnlyReachesNoPipelineCode(unittest.TestCase):
    def test_delegation_happens_before_any_lane_runner_is_built(self):
        """The branch must precede `lane_runners` construction, not merely the
        pipeline entry.

        The first version of this test asserted only that the branch came before the
        run-lock import -- which it did, while sitting AFTER
        `lane_runners["fantastic"] = real_fantastic_runner()`. The runners are lazy
        closures so nothing was contacted, but "no acquisition code is reachable" was
        not what the code did. The assertion now matches the claim.
        """
        import inspect

        source = inspect.getsource(run_orchestrator)
        branch = source.index('getattr(config, "MAINTENANCE_ONLY"')
        lanes = source.index("lane_runners: Dict[str, Any] = {}")
        entry = source.index("from orchestrator.runlock import RunLockHeld")
        self.assertLess(branch, lanes,
                        "maintenance must return before any lane runner is built")
        self.assertLess(branch, entry)

    def test_it_delegates_to_the_narrow_entry_point(self):
        import inspect

        source = inspect.getsource(run_orchestrator)
        head = source[:source.index("from orchestrator.runlock import RunLockHeld")]
        self.assertIn("import run_maintenance", head)
        self.assertIn("run_maintenance.main(", head)

    def test_the_maintenance_module_reaches_no_provider(self):
        """Structural: no acquisition adapter, no Apollo, no Hunter, no delivery."""
        import run_maintenance

        source = open(run_maintenance.__file__, encoding="utf-8").read()
        for forbidden in ("fantastic_jobs_adapter", "apollo_client", "hunter_client",
                          "instantly_client", "real_fantastic_runner", "RealDelivery"):
            self.assertNotIn(forbidden, source,
                             f"maintenance must not reach {forbidden}")

        # The guard is about IMPORTS AND CALLS, not vocabulary. It used to forbid the
        # bare substring "hunter", which also forbade reading a `hunter_status` FIELD
        # out of a stored artifact -- a string in a JSON file that contacts nobody.
        # Checking the import graph says what was meant; checking the word said
        # something else and would have been "fixed" by renaming a local.
        import ast

        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for module in ("apollo_client", "hunter_client", "instantly_client",
                       "fantastic_jobs_adapter"):
            self.assertNotIn(module, imported,
                             f"maintenance must not import {module}")

    def test_it_calls_no_write_entry_point(self):
        """`airtable_client` IS imported -- for `_company_identity_keys_from_job`,
        the production key the suppression rule uses, which capacity measurement must
        not re-implement. What must never appear is a CALL that writes."""
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called.add(getattr(fn, "id", None) or getattr(fn, "attr", None) or "")
        for forbidden in ("push_leads", "enroll_record", "enroll_approved_leads",
                          "create_leads", "deliver"):
            self.assertNotIn(forbidden, called,
                             f"maintenance must never call {forbidden}")

    def test_only_pure_helpers_are_taken_from_airtable_client(self):
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "airtable_client":
                imported.update(a.name for a in node.names)
        # PURE helpers only. The guard's purpose is that maintenance can never reach
        # the WRITER: `_job_to_fields` and `send_safe_facts` build and inspect a row
        # in memory and make no HTTP call, so asking the gate why a lead was withheld
        # is a read. `push_leads` and anything that issues a request stay forbidden,
        # and a separate test asserts none of them is called.
        self.assertTrue(imported <= {"_company_identity_keys_from_job",
                                     "_job_to_fields", "send_safe_facts"},
                        f"unexpected airtable_client imports: {imported}")
        for writer in ("push_leads", "request_with_retry", "_base_url", "_headers"):
            self.assertNotIn(writer, imported)

    def test_it_never_sends_slack(self):
        """Checked as CODE, not as prose: no slack module, no webhook read, no
        delivery call. The docstring is allowed to mention it; the module is not
        allowed to do it."""
        import ast

        import run_maintenance

        tree = ast.parse(open(run_maintenance.__file__, encoding="utf-8").read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        offenders = {n for n in names if "slack" in n.lower() or "webhook" in n.lower()}
        self.assertEqual(offenders, set(), f"maintenance must not reach {offenders}")


class TheMaintenancePassIsOrderedBackupFirst(unittest.TestCase):
    def test_backup_precedes_adoption_in_the_entry_point(self):
        import inspect

        import run_maintenance

        source = inspect.getsource(run_maintenance.main)
        self.assertLess(source.index("BACKUP"), source.index("ADOPT"),
                        "evidence is copied before anything mutates it")
        self.assertLess(source.index("ADOPT"), source.index("REPORTING"),
                        "custody is taken before the report reads the store")


if __name__ == "__main__":
    unittest.main()


class TheProvenanceProbeExplainsAPartialMetric(unittest.TestCase):
    """A `partial` status says a contribution is missing; it cannot say whether the
    field is RECOVERABLE from an unread payload or was never recorded. Those need
    opposite responses -- read it, or say why it cannot be read -- and guessing
    between them is how a reporting gap gets reported as a data gap."""

    def _root(self, **runs):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        for run_id, result in runs.items():
            d = root / "run_artifacts" / run_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "orchestrator_result.json").write_text(_json.dumps(result),
                                                        encoding="utf-8")
        return root

    def test_a_recorded_field_is_reported_present(self):
        root = self._root(**{"r1": {"enrichment": {"funnel": {
            "qualification_input": 226, "contact_discovery_entered": 100}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["funnel_qualification_input"], 226)
        self.assertEqual(row["funnel_contact_discovery_entered"], 100)

    def test_an_absent_field_is_reported_absent_with_what_the_run_does_carry(self):
        """`funnel_keys` is the actionable half: it distinguishes "the funnel was
        never written" from "the funnel was written without this counter"."""
        root = self._root(**{"r1": {"enrichment": {"funnel": {"captured": 6205}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertIsNone(row["funnel_qualification_input"])
        self.assertEqual(row["funnel_keys"], ["captured"])

    def test_a_run_with_no_result_at_all_still_reports_a_row(self):
        root = self._root()
        (root / "run_artifacts" / "empty").mkdir(parents=True, exist_ok=True)
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["run_id"], "empty")
        self.assertIsNone(row["funnel_qualification_input"])

    def test_zero_only_skip_buckets_are_flagged_not_listed(self):
        root = self._root(**{"r1": {"delivery": {"skip_breakdown": {
            "account_suppressed": 0, "not_final_pass": 0}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["skip_breakdown_nonzero"], {})
        self.assertTrue(row["skip_breakdown_all_zero"])

    def test_a_firing_skip_bucket_is_listed(self):
        root = self._root(**{"r1": {"delivery": {"skip_breakdown": {
            "account_suppressed": 0, "company_function_suppressed": 200}}}})
        row = run_maintenance.provenance_probe(root)["runs"][0]
        self.assertEqual(row["skip_breakdown_nonzero"],
                         {"company_function_suppressed": 200})
        self.assertFalse(row["skip_breakdown_all_zero"])


class CustodyIsProvedResumableNotJustCounted(unittest.TestCase):
    """`pending_postings 3595` is a file statistic. It says nothing about whether
    what is held can be handed back and processed -- a store full of stubs would
    report the same number and fail on the day it was needed. The dry run loads the
    work through the pipeline's own `pending_work.load` and runs the production
    identity functions over what comes back."""

    def _store(self, jobs, run_id="20260904T130130Z-13b44a0c"):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from orchestrator import pending_work as pw

        root = _Path(_tempfile.mkdtemp())
        pw.record(root / pw.STORE, run_id, jobs)
        return root

    def _job(self, jid, employer="Acme Robotics", title="Head of Sales",
             domain="acme.example"):
        return {"job_id": jid, "posting_id": jid, "title": title,
                "employer_name": employer, "company_name": employer,
                "organization": employer, "employer_website": domain,
                "url": f"https://{domain}/{jid}"}

    def test_held_work_is_loaded_through_the_pipelines_own_reader(self):
        root = self._store([self._job("j1"), self._job("j2")])
        out = run_maintenance.resume_dry_run(root)
        self.assertEqual(out["loaded_via"], "orchestrator.pending_work.load")
        self.assertEqual(out["returned"], 2)

    def test_it_reports_opportunities_not_only_postings(self):
        """The unit that bounds approvals, so the preserved backlog can be stated as
        business capacity rather than as a row count."""
        root = self._store([self._job("j1"), self._job("j2"),
                            self._job("j3", employer="Other Corp",
                                      domain="other.example")])
        ident = run_maintenance.resume_dry_run(root)["identities"]
        self.assertEqual(ident["postings"], 3)
        self.assertGreaterEqual(ident["companies"], 2)
        self.assertGreaterEqual(ident["opportunities"], 2)

    def test_a_payload_with_no_resolvable_employer_is_reported_unresumable(self):
        """It could never finish, so it would sit in custody for ever -- the one
        failure mode a count of held rows cannot show."""
        root = self._store([{"job_id": "x1", "posting_id": "x1", "title": "Head of Sales"}])
        out = run_maintenance.resume_dry_run(root)
        self.assertFalse(out["resumable"])
        self.assertIn("no resolvable employer", out["note"])

    def test_an_empty_store_is_not_reported_as_a_failure(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        out = run_maintenance.resume_dry_run(_Path(_tempfile.mkdtemp()))
        self.assertEqual(out["returned"], 0)
        self.assertEqual(out["note"], "no work is held")

    def test_the_dry_run_releases_nothing(self):
        from orchestrator import pending_work as pw

        root = self._store([self._job("j1"), self._job("j2")])
        before = pw.summary(root / pw.STORE)["pending_postings"]
        run_maintenance.resume_dry_run(root)
        self.assertEqual(pw.summary(root / pw.STORE)["pending_postings"], before)


class BothCohortsAreCountedByOneImplementation(unittest.TestCase):
    def test_capacity_and_the_dry_run_share_the_identity_helper(self):
        """Two implementations of "an opportunity" would make the retained-payload
        figure and the custody figure incomparable -- and they are quoted together."""
        import inspect

        source = inspect.getsource(run_maintenance)
        self.assertEqual(source.count("def measure_identities"), 1)
        self.assertIn("row.update(measure_identities(jobs))", source)
        self.assertIn('out["identities"] = measure_identities(jobs)', source)


class TheDurableRecordIsRefreshedWhileTheEvidenceExists(unittest.TestCase):
    """The A/B runs on a COPY, so it proves the ledger *can* answer without proving
    the production ledger *does*. Only a pipeline run backfills the real one, and
    while acquisition is paused no pipeline runs -- so a corrected loss-reason census
    would pass its tests and never reach the record that outlives the artifacts.
    Friday's report reads that record.

    The prior entry here is written by the REAL backfill rather than hand-built, so
    "an entry that already existed" means an entry the production writer produced.
    """

    RUN_ID = "20260904T130130Z-13b44a0c"
    RECONCILING = {"reviewable_submitted": 100, "created": 60, "failed": 0,
                   "skip_breakdown": {"no_contact": 40},
                   "reviewable_reconciles": True}
    UNRECONCILED = {"reviewable_submitted": 1681, "created": 781, "failed": 0,
                    "skip_breakdown": {"account_suppressed": 0},
                    "reviewable_reconciles": False}

    def _root(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / self.RUN_ID
        d.mkdir(parents=True, exist_ok=True)
        (d / "orchestrator_result.json").write_text("{}", encoding="utf-8")
        (d / "run_status.json").write_text(_json.dumps({"status": "complete"}),
                                           encoding="utf-8")
        return root

    def _delivery(self, root, payload):
        import json as _json

        (root / "run_artifacts" / self.RUN_ID / "delivery.json").write_text(
            _json.dumps(payload), encoding="utf-8")

    def test_a_correction_reaches_an_entry_that_already_existed(self):
        root = self._root()
        self._delivery(root, self.RECONCILING)
        run_maintenance.refresh_ledger(root)          # the entry now exists
        self._delivery(root, self.UNRECONCILED)       # ...and the evidence changes
        out = run_maintenance.refresh_ledger(root)
        self.assertIn(self.RUN_ID, out["loss_reasons_changed"])
        change = out["loss_reasons_changed"][self.RUN_ID]
        self.assertEqual(change["before"], {"no_contact": 40})
        self.assertEqual(change["after"], {"delivery_unreconciled": 900})

    def test_it_reports_nothing_when_the_record_is_already_current(self):
        root = self._root()
        self._delivery(root, self.UNRECONCILED)
        run_maintenance.refresh_ledger(root)
        self.assertEqual(run_maintenance.refresh_ledger(root)["loss_reasons_changed"],
                         {})

    def test_an_unreadable_ledger_file_is_reported_not_swallowed(self):
        root = self._root()
        self._delivery(root, self.UNRECONCILED)
        run_maintenance.refresh_ledger(root)
        from orchestrator.run_ledger import LEDGER_STORE
        (root / LEDGER_STORE / "not-an-entry.json").write_text("{{{", encoding="utf-8")
        self.assertTrue(run_maintenance.refresh_ledger(root)["unreadable_entries"])


class ThePreApolloHalfOfTheFunnelIsMeasurableWithoutApollo(unittest.TestCase):
    """Conversion is the binding constraint on 1,000 approved/day -- inventory is
    not -- and the only observed figure comes from a run Apollo truncated. Apollo
    cannot be re-run, but the first half of that funnel never calls it: JobGate and
    RoleGate over the retained postings, with `fetch_sources=False` making no network
    request at all. So "where are opportunities lost before a credit is spent" is
    answerable today, on the real cohort, for nothing."""

    def _root(self, jobs):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / "r1" / "enrichment"
        d.mkdir(parents=True, exist_ok=True)
        (d / "postings.json").write_text(_json.dumps({"jobs": jobs}), encoding="utf-8")
        return root

    def _job(self, jid, title="Head of Sales", employer="Acme Robotics"):
        return {"job_id": jid, "posting_id": jid, "job_title": title, "title": title,
                "employer_name": employer, "company_name": employer,
                "employer_website": "acme.example",
                "job_apply_link": f"https://acme.example/{jid}"}

    def test_a_missing_payload_is_reported_not_crashed(self):
        out = run_maintenance.qualify_offline(self._root([]), ["absent-run"])
        self.assertEqual(out["runs"][0]["unavailable"], "postings.json absent")

    def test_it_reports_the_gate_outcome_for_a_real_payload(self):
        out = run_maintenance.qualify_offline(self._root(
            [self._job("j1"), self._job("j2")]), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row.get("unavailable", ""), "")
        self.assertEqual(row["input_jobs"], 2)
        for key in ("contact_eligible_jobs", "rejected_jobs", "needs_check_jobs"):
            self.assertIsInstance(row[key], int)

    def test_zero_count_reasons_are_omitted(self):
        """Same rule the weekly report now applies: a reason that did not fire
        explains nothing, and a fixed-shape stats dict emits them all."""
        out = run_maintenance.qualify_offline(self._root([self._job("j1")]), ["r1"])
        self.assertTrue(all(v > 0 for v in out["runs"][0]["reasons"].values()))

    def test_nothing_is_written_under_the_artifact_root(self):
        """The gates write a filtered corpus; none of it may land in production
        state, so the output goes to a throwaway directory."""
        root = self._root([self._job("j1")])
        before = {p.name for p in (root / "run_artifacts" / "r1").iterdir()}
        run_maintenance.qualify_offline(root, ["r1"])
        after = {p.name for p in (root / "run_artifacts" / "r1").iterdir()}
        self.assertEqual(before, after)

    def test_it_makes_no_network_request(self):
        """Pinned on the call: `fetch_sources=False` is what guarantees it, and a
        replay that fetched sources would be an acquisition run wearing a
        measurement's name."""
        import inspect

        source = inspect.getsource(run_maintenance.qualify_offline)
        self.assertIn("fetch_sources=False", source)


class NotAllOfContactDiscoveryIsApollo(unittest.TestCase):
    """The 25.3% opportunity -> contact rate has been attributed wholesale to Apollo.
    Part of it never reaches Apollo: `_process_company` computes
    `_best_input_domain(job)` from the posting itself, and an opportunity with no
    resolvable search domain is recorded `missing_company_domain` without a people
    search ever running.

    Splitting the two matters because they have different owners. A missing
    first-party domain is an INTERNAL loss addressable by domain resolution; a
    person Apollo cannot find is not. Attributing both to billing would leave the
    fixable half unfixed."""

    def _root(self, jobs, run_id="r1"):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / run_id / "enrichment"
        d.mkdir(parents=True, exist_ok=True)
        (d / "postings.json").write_text(_json.dumps({"jobs": jobs}), encoding="utf-8")
        return root

    def _job(self, jid, employer, domain=""):
        job = {"job_id": jid, "posting_id": jid, "job_title": "Head of Sales",
               "title": "Head of Sales", "employer_name": employer,
               "company_name": employer, "organization": employer}
        if domain:
            job["_employer_domain_input"] = domain
            job["employer_website"] = domain
            job["job_apply_link"] = f"https://{domain}/{jid}"
        return job

    def test_a_company_with_its_own_domain_is_counted_ready(self):
        out = run_maintenance.domain_readiness(
            self._root([self._job("j1", "Acme Robotics", "acme.example")]), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["companies_with_first_party_domain"], 1)
        self.assertEqual(row["opportunities_depending_on_apollo_for_a_domain"], 0)

    def test_a_company_without_one_depends_on_the_provider(self):
        out = run_maintenance.domain_readiness(
            self._root([self._job("j1", "Nameless Co")]), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["companies_without"], 1)
        self.assertEqual(row["opportunities_with_first_party_domain"], 0)

    def test_an_employer_that_splits_is_flagged_as_recoverable(self):
        """`company_key_for_job` is "domain or name", so ONE employer becomes TWO
        groups when only some of its postings carry a domain. The domainless half
        then spends an Apollo organisation enrich and returns
        `missing_company_domain` -- while the domain sits on a sibling posting we
        already hold. That subset needs no provider at all to fix."""
        out = run_maintenance.domain_readiness(self._root([
            self._job("j1", "Acme Robotics", "acme.example"),
            self._job("j2", "Acme Robotics"),
        ]), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["companies"], 2, "this split is the production behaviour")
        self.assertEqual(row["recoverable_companies_same_name_has_domain"], 1)
        self.assertGreaterEqual(row["recoverable_opportunities"], 1)

    def test_an_unrelated_domainless_employer_is_not_called_recoverable(self):
        out = run_maintenance.domain_readiness(self._root([
            self._job("j1", "Acme Robotics", "acme.example"),
            self._job("j2", "Nameless Co"),
        ]), ["r1"])
        self.assertEqual(out["runs"][0]["recoverable_companies_same_name_has_domain"], 0)

    def test_a_missing_payload_is_reported(self):
        out = run_maintenance.domain_readiness(self._root([]), ["nope"])
        self.assertEqual(out["runs"][0]["unavailable"], "postings.json absent")

    def test_it_calls_no_provider_client(self):
        """A measurement that quietly enriched would be an Apollo run wearing a
        probe's name -- and Apollo is refusing, so it would also just fail."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(run_maintenance.domain_readiness)))
        called = {getattr(n.func, "attr", "") for n in ast.walk(tree)
                  if isinstance(n, ast.Call)}
        # `dict.get` is obviously fine; these are the provider entry points.
        for forbidden in ("enrich_organization", "search_people_at_company",
                          "match_person", "request_with_retry"):
            self.assertNotIn(forbidden, called)


class ARateNeedsADenominatorFromTheStageThatIssuesTheSearch(unittest.TestCase):
    """`domain_readiness` found 99% of opportunities carried a first-party domain and
    that was read as "they reached Apollo". It is not. A domain makes an opportunity
    ELIGIBLE to be searched; the 2026-09-04 run was interrupted partway through
    contact discovery, so an unknown share was never attempted -- and dividing
    contacts by eligible opportunities counts never-attempted work as a
    hiring-manager failure.

    The denominator can only come from `contact_discovery_entered`, emitted by
    `hiring_manager` at the people-search decision point. When it is absent the rate
    is UNKNOWN, and no payload recount may stand in for it: a recount cannot tell
    "searched and found nobody" from "the run stopped first", which is the whole
    question.
    """

    def _root(self, *, funnel=None, waterfall=None, status=None, jobs=None):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / "r1"
        (d / "enrichment").mkdir(parents=True, exist_ok=True)
        (d / "orchestrator_result.json").write_text(
            _json.dumps({"enrichment": {"funnel": funnel or {}}}), encoding="utf-8")
        if waterfall is not None:
            (d / "waterfall.json").write_text(_json.dumps(waterfall), encoding="utf-8")
        (d / "run_status.json").write_text(_json.dumps(status or {}), encoding="utf-8")
        (d / "enrichment" / "postings.json").write_text(
            _json.dumps({"jobs": jobs or []}), encoding="utf-8")
        return root

    def _hm(self, **kw):
        stage = {"stage": "hiring_manager", "unit": "lead", "entered": 0,
                 "passed": 0, "rejected": 0, "deferred": 0, "errored": 0,
                 "primary_reasons": {}}
        stage.update(kw)
        return {"stages": [stage]}

    def test_an_absent_stage_entry_makes_the_rate_unknown(self):
        out = run_maintenance.execution_reconcile(
            self._root(funnel={}, waterfall=self._hm()), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["rate_status"], "unknown")
        self.assertIsNone(row["opportunity_to_contact_rate"])
        self.assertIsNone(row["opportunities_searched"])
        self.assertTrue(any("contact_discovery_entered absent" in u
                            for u in row["unavailable"]))

    def test_never_attempted_is_unknown_rather_than_the_whole_population(self):
        """The tempting wrong answer: held minus contacts. That silently asserts
        everything not converted was attempted."""
        job = {"job_id": "j1", "job_title": "Head of Sales", "title": "Head of Sales",
               "employer_name": "Acme", "company_name": "Acme",
               "_employer_domain_input": "acme.example"}
        out = run_maintenance.execution_reconcile(
            self._root(funnel={}, waterfall=self._hm(), jobs=[job]), ["r1"])
        row = out["runs"][0]
        self.assertIsNotNone(row["opportunities_retained"])
        self.assertIsNone(row["never_attempted"])

    def test_a_recorded_stage_entry_yields_a_measured_rate(self):
        out = run_maintenance.execution_reconcile(self._root(
            funnel={"contact_discovery_entered": 2000},
            waterfall={"contacts_found": 500, "stages": [self._hm()["stages"][0]]},
        ), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["rate_status"], "measured")
        self.assertEqual(row["opportunities_searched"], 2000)
        self.assertEqual(row["opportunity_to_contact_rate"], 0.25)
        self.assertEqual(row["denominator_source"],
                         "enrichment.funnel.contact_discovery_entered")

    def test_outcomes_are_split_by_who_owns_them(self):
        out = run_maintenance.execution_reconcile(self._root(
            funnel={"contact_discovery_entered": 100},
            waterfall=self._hm(entered=100, primary_reasons={
                "hiring_manager_not_found": 40, "not_icp": 20,
                "stage_error": 5, "some_new_code": 7, "already_delivered": 0}),
        ), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["genuine_no_match"], 40)
        self.assertEqual(row["internal_skips"], 20)
        self.assertEqual(row["provider_errors"], 5)
        self.assertEqual(row["unclassified_reasons"], {"some_new_code": 7})

    def test_an_unknown_reason_is_surfaced_not_folded_into_a_bucket(self):
        """A code this file has not seen must not be silently counted as somebody
        else's fault."""
        out = run_maintenance.execution_reconcile(self._root(
            funnel={"contact_discovery_entered": 10},
            waterfall=self._hm(primary_reasons={"brand_new_reason": 3}),
        ), ["r1"])
        row = out["runs"][0]
        self.assertEqual(row["genuine_no_match"], 0)
        self.assertEqual(row["internal_skips"], 0)
        self.assertEqual(row["provider_errors"], 0)
        self.assertIn("brand_new_reason", row["unclassified_reasons"])

    def test_a_missing_waterfall_stage_is_reported_as_missing(self):
        out = run_maintenance.execution_reconcile(
            self._root(funnel={}, waterfall={"stages": []}), ["r1"])
        row = out["runs"][0]
        self.assertIsNone(row["hiring_manager_stage"])
        self.assertTrue(any("no hiring_manager stage" in u for u in row["unavailable"]))


class TheStageIdentityIsRestatedAndEmailUnverifiedIsItsOwnOutcome(unittest.TestCase):
    """Two corrections the 2026-09-04 reconciliation forced.

    `email_unverified` was filed under provider errors. It means a person WAS found
    and their address could not be promoted to verified -- neither "nobody there" nor
    "the provider broke", and the largest single outcome on that run at 740 of 2,410.
    Its remedy is nothing like the other two, so conflating them would have pointed
    the next fix at the wrong stage.

    And the buckets must add up to what the stage sealed. A classification that
    silently drops a reason is worse than no classification, because it reads as
    completeness."""

    def _run(self, reasons, **stage):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / "r1"
        (d / "enrichment").mkdir(parents=True, exist_ok=True)
        (d / "orchestrator_result.json").write_text("{}", encoding="utf-8")
        (d / "run_status.json").write_text("{}", encoding="utf-8")
        (d / "enrichment" / "postings.json").write_text(
            _json.dumps({"jobs": []}), encoding="utf-8")
        base = {"stage": "hiring_manager", "unit": "lead", "entered": 0, "passed": 0,
                "rejected": 0, "deferred": 0, "errored": 0, "primary_reasons": reasons}
        base.update(stage)
        (d / "waterfall.json").write_text(_json.dumps({"stages": [base]}),
                                          encoding="utf-8")
        return run_maintenance.execution_reconcile(root, ["r1"])["runs"][0]

    def test_email_unverified_is_not_a_provider_error(self):
        row = self._run({"email_unverified": 740}, entered=740, deferred=740)
        self.assertEqual(row["contact_found_email_unverified"], 740)
        self.assertEqual(row["provider_errors"], 0)
        self.assertEqual(row["genuine_no_match"], 0)

    def test_the_production_shape_reconciles_against_the_sealed_stage(self):
        """The real 2026-09-04 numbers: 712 rejected + 987 deferred, and the four
        reasons that produced them."""
        row = self._run({"not_icp": 606, "company_unresolved": 106,
                         "hiring_manager_not_found": 247, "email_unverified": 740},
                        entered=2410, passed=711, rejected=712, deferred=987)
        self.assertEqual(row["internal_skips"], 712)
        self.assertEqual(row["genuine_no_match"], 247)
        self.assertEqual(row["contact_found_email_unverified"], 740)
        self.assertTrue(row["reasons_reconcile"])

    def test_a_dropped_reason_makes_the_identity_fail_loudly(self):
        row = self._run({"not_icp": 10}, entered=100, rejected=50)
        self.assertFalse(row["reasons_reconcile"])

    def test_leads_the_stage_never_produced_are_counted_as_a_lower_bound(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        d = root / "run_artifacts" / "r1"
        (d / "enrichment").mkdir(parents=True, exist_ok=True)
        (d / "orchestrator_result.json").write_text("{}", encoding="utf-8")
        (d / "run_status.json").write_text("{}", encoding="utf-8")
        jobs = [{"job_id": f"j{i}", "job_title": "Head of Sales",
                 "title": "Head of Sales", "employer_name": f"Co{i}",
                 "company_name": f"Co{i}", "_employer_domain_input": f"c{i}.example"}
                for i in range(5)]
        (d / "enrichment" / "postings.json").write_text(
            _json.dumps({"jobs": jobs}), encoding="utf-8")
        (d / "waterfall.json").write_text(_json.dumps({"stages": [
            {"stage": "hiring_manager", "unit": "lead", "entered": 2, "passed": 2,
             "rejected": 0, "deferred": 0, "errored": 0, "primary_reasons": {}}]}),
            encoding="utf-8")
        row = run_maintenance.execution_reconcile(root, ["r1"])["runs"][0]
        self.assertEqual(row["opportunities_never_reaching_the_stage"], 3)
        self.assertIsNone(row["opportunity_to_contact_rate"],
                          "a lower bound is still not a denominator")


class OutcomeForensicsDecomposesOnlyWhatTheArtifactsSupport(unittest.TestCase):
    """606 `not_icp`, 106 `company_unresolved` and 740 `email_unverified` are each a
    bucket of several situations with different owners and different remedies --
    `email_unverified` alone covers a missing address, one Apollo returned without
    verifying, one our own generic-mailbox or domain-identity policy rejected, and one
    a second opinion called undeliverable.

    A plausible split is indistinguishable from a measured one once it is written
    down, so anything the files cannot separate is listed as not decomposable rather
    than apportioned."""

    def _root(self, files):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        enr = root / "run_artifacts" / "r1" / "enrichment"
        enr.mkdir(parents=True, exist_ok=True)
        for name, payload in files.items():
            (enr / name).write_text(_json.dumps(payload), encoding="utf-8")
        return root

    def test_a_run_with_no_per_lead_rows_says_so(self):
        root = self._root({"postings.json": {"jobs": []}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertTrue(any("no per-lead rows" in n for n in row["not_decomposable"]))
        self.assertTrue(any("not_icp total cannot be attributed" in n
                            for n in row["not_decomposable"]))

    def test_icp_reason_families_are_read_from_the_stage_stats(self):
        root = self._root({"step3_stats.json": {
            "company_criteria_reason__headcount": 400,
            "company_criteria_reason__industry": 206,
            "unrelated_counter": 9}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["icp_reason_families"],
                         {"headcount": 400, "industry": 206})

    def test_email_outcomes_split_missing_address_from_unverified_one(self):
        root = self._root({"leads.json": {"leads": [
            {"hm_reason": "email_unverified", "email": ""},
            {"hm_reason": "email_unverified", "email": "a@x.com",
             "email_status": "extrapolated"},
            {"hm_reason": "email_unverified", "email": "b@x.com",
             "email_status": "unverified"},
        ]}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["email_unverified_breakdown"]["no_address"], 1)
        self.assertEqual(
            row["email_unverified_breakdown"]["address_apollo_status=extrapolated"], 1)

    def test_whether_a_second_opinion_ran_is_reported(self):
        """`VERIFY_WITH_HUNTER` differs between the two services, so "was there a
        second opinion at all" is a question the artifacts have to answer."""
        root = self._root({"leads.json": {"leads": [
            {"hm_reason": "email_unverified", "email": "a@x.com"},
            {"hm_reason": "email_unverified", "email": "b@x.com",
             "metadata": {"hunter_status": "valid"}},
        ]}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["hunter_status"]["(absent)"], 1)
        self.assertEqual(row["hunter_status"]["valid"], 1)

    def test_reads_the_fields_the_hiring_manager_actually_persists(self):
        root = self._root({"jobs_enriched_2026-09-06.json": {"jobs": [
            {"job_id": "j1", "hiring_manager_email": "a@x.com",
             "apollo_email_status": "unverified", "hunter_email_status": "valid",
             "_final_primary_reason": "unverified_email_deliverability"}]}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["hunter_status"], {"valid": 1})
        self.assertEqual(row["email_unverified_breakdown"], {"address_apollo_status=unverified": 1})

    def test_the_giant_postings_file_is_not_parsed_as_lead_rows(self):
        root = self._root({"postings.json": {"jobs": [{"job_id": "j1"}] * 3},
                           "leads.json": {"leads": [{"hm_reason": "not_icp"}]}})
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["rows_scanned"], 1)


class LargeCorporaAreStreamedNotSkipped(unittest.TestCase):
    """The first forensics run reported "no per-lead rows" for a run whose enrichment
    directory holds a 143 MB enriched-lead corpus and a 90 MB progress file. It had
    skipped both for being large -- discarding the only evidence that could answer
    the question, and then reporting the absence as a property of the run.

    Large files are counted by a chunked scan for literal `"field": "value"`
    occurrences: no parser, bounded memory. It counts FIELD OCCURRENCES rather than
    objects and is labelled that way, which makes it an upper bound per lead -- the
    safe direction for "this outcome exists in quantity"."""

    def _file(self, payload):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        path = _Path(_tempfile.mkdtemp()) / "big.json"
        path.write_text(_json.dumps(payload), encoding="utf-8")
        return path

    def test_it_counts_values_without_parsing(self):
        path = self._file({"leads": [{"email_status": "verified"},
                                     {"email_status": "extrapolated"},
                                     {"email_status": "verified"}]})
        counts = run_maintenance._stream_field_counts(path, ("email_status",))
        self.assertEqual(counts["email_status"], {"verified": 2, "extrapolated": 1})

    def test_a_match_split_across_a_chunk_boundary_is_not_lost(self):
        """The bug a naive chunked scanner has, and the reason for the carry window."""
        path = self._file({"leads": [{"email_status": "verified"} for _ in range(40)]})
        whole = run_maintenance._stream_field_counts(path, ("email_status",))
        tiny = run_maintenance._stream_field_counts(path, ("email_status",), chunk=17)
        self.assertEqual(whole["email_status"]["verified"], 40)
        self.assertEqual(tiny["email_status"]["verified"], 40)

    def test_an_absent_field_yields_no_entry_rather_than_a_zero(self):
        path = self._file({"leads": [{"other": "x"}]})
        self.assertEqual(run_maintenance._stream_field_counts(path, ("email_status",)),
                         {})

    def test_an_unreadable_file_returns_empty_rather_than_raising(self):
        from pathlib import Path as _Path

        self.assertEqual(
            run_maintenance._stream_field_counts(_Path("no-such-file.json"),
                                                 ("email_status",)), {})

    def test_icp_reason_families_are_found_when_nested(self):
        """`hiring_manager_summary.json` is 936 bytes and was reported as carrying no
        ICP stats because the first version looked only at the top level."""
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        enr = root / "run_artifacts" / "r1" / "enrichment"
        enr.mkdir(parents=True, exist_ok=True)
        (enr / "hiring_manager_summary.json").write_text(_json.dumps(
            {"stats": {"nested": {"company_criteria_reason__headcount": 400}}}),
            encoding="utf-8")
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["icp_reason_families"], {"headcount": 400})


class StreamedCountsAreNotSummedAcrossFiles(unittest.TestCase):
    """The 2026-09-04 run writes the same leads into `enrichment_progress.json` AND
    `jobs_enriched_*.json`. Folding both into one aggregate reported
    `apollo_email_status verified: 2068` for 1,034 leads -- each counted twice, in a
    number that reads like a population."""

    def test_per_file_counts_are_kept_and_not_aggregated(self):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        enr = root / "run_artifacts" / "r1" / "enrichment"
        enr.mkdir(parents=True, exist_ok=True)
        payload = {"leads": [{"email_status": "verified"} for _ in range(3)]}
        blob = _json.dumps(payload) + " " * 26_000_000
        for name in ("enrichment_progress.json", "jobs_enriched_2026-09-04.json"):
            (enr / name).write_text(blob, encoding="utf-8")

        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(len(row["streamed_large_files"]), 2)
        for entry in row["streamed_large_files"]:
            self.assertEqual(entry["counts"]["email_status"], {"verified": 3})
        self.assertEqual(row["apollo_email_status"], {},
                         "streamed counts must not inflate the aggregate")

    def test_small_duplicate_snapshots_are_also_kept_per_file(self):
        import json
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
        enr = root / "run_artifacts" / "r1" / "enrichment"
        enr.mkdir(parents=True)
        payload = {"stats": {"company_criteria_reason__headcount": 1},
                   "leads": [{"email_status": "verified", "hm_reason": "email_unverified"}]}
        for name in ("checkpoint.json", "final.json"):
            (enr / name).write_text(json.dumps(payload))
        row = run_maintenance.outcome_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["apollo_email_status"], {})
        self.assertEqual(row["icp_reason_families"], {})
        self.assertEqual(len(row["parsed_files"]), 2)
        for file in row["parsed_files"]:
            self.assertEqual(file["apollo_email_status"], {"verified": 1})
        self.assertEqual(row["aggregation_status"]["apollo_email_status"], "multiple_files_unreconciled")


class WithholdingIsExplainedPerLeadNotCounted(unittest.TestCase):
    """The 2026-09-06 calibration produced two verified contacts, created zero rows,
    and recorded only `send_safe_withheld: 2`. A count cannot explain a withholding,
    and neither can a larger budget -- the two are unrelated.

    `send_safe_facts` is deterministic, offline and fail-closed, returning the FIRST
    failing fact. Rebuilding a retained lead's fields with the production
    `_job_to_fields` and re-asking the gate reproduces exactly the decision delivery
    made, with the reason it never wrote down."""

    def _root(self, jobs, run_id="r1"):
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        root = _Path(_tempfile.mkdtemp())
        enr = root / "run_artifacts" / run_id / "enrichment"
        enr.mkdir(parents=True, exist_ok=True)
        (enr / "jobs_enriched_2026-09-06.json").write_text(
            _json.dumps({"jobs": jobs}), encoding="utf-8")
        return root

    def test_a_run_with_no_per_lead_rows_says_it_cannot_explain(self):
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
        (root / "run_artifacts" / "r1" / "enrichment").mkdir(parents=True)
        row = run_maintenance.send_safe_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["examined"], 0)
        self.assertTrue(any("cannot be explained" in u for u in row["unavailable"]))

    def test_every_examined_lead_gets_a_named_first_failing_fact(self):
        root = self._root([{"job_id": "j1", "employer_name": "Acme",
                            "company_name": "Acme", "job_title": "Head of Sales"}])
        row = run_maintenance.send_safe_forensics(root, ["r1"])["runs"][0]
        self.assertEqual(row["examined"], 1)
        self.assertEqual(row["withheld"], 1)
        self.assertTrue(row["reasons"], "a withholding without a reason is the bug")
        self.assertNotIn("", row["reasons"])

    def test_a_verified_contact_that_is_still_withheld_is_singled_out(self):
        """The exact unexplained population from the calibration."""
        root = self._root([{
            "job_id": "j1", "employer_name": "Acme", "company_name": "Acme",
            "job_title": "Head of Sales",
            "hiring_manager_email": "hm@acme.example",
            "apollo_email_status": "verified",
        }])
        row = run_maintenance.send_safe_forensics(root, ["r1"])["runs"][0]
        if row["verified_withheld"]:
            entry = row["verified_withheld"][0]
            self.assertTrue(entry["reason"])
            self.assertIn("outbound_hold", entry)

    def test_it_never_writes_or_repairs_anything(self):
        """A rebuilt row is used to ASK the gate, never to change it -- and patching
        a signed display field without re-signing is a known way to break approval."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(run_maintenance.send_safe_forensics)))
        called = {getattr(n.func, "attr", "") for n in ast.walk(tree)
                  if isinstance(n, ast.Call)}
        for forbidden in ("push_leads", "request_with_retry", "patch", "update",
                          "create", "write_text"):
            self.assertNotIn(forbidden, called)
