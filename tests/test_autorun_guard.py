"""Universal autorun-guard regression + Start-Command contract tests.

Proves the pipeline cannot execute from ANY entry path unless
PIPELINE_AUTORUN_ENABLED is an explicit enabled token, including a real
subprocess of the exact Railway Start Command `python -u run_daily.py`.
No network/provider call is made (the guard prevents execution).
"""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import config
import run_daily

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DISABLED = ("", "0", "false", "no", "off", "  ", "maybe", "2", "enabled")
_ENABLED = ("1", "true", "yes", "on", "TRUE", "On", " 1 ")


class StrictBooleanParsingTests(unittest.TestCase):
    def test_enabled_tokens(self):
        for tok in _ENABLED:
            with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": tok}):
                self.assertTrue(config.autorun_is_enabled(), tok)

    def test_disabled_and_invalid_tokens(self):
        for tok in _DISABLED:
            with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": tok}):
                self.assertFalse(config.autorun_is_enabled(), repr(tok))

    def test_missing_is_disabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_AUTORUN_ENABLED", None)
            self.assertFalse(config.autorun_is_enabled())

    def test_zero_never_truthy(self):
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "0"}):
            self.assertFalse(config.autorun_is_enabled())


class RunPipelineChokepointTests(unittest.TestCase):
    def test_disabled_run_pipeline_no_side_effects(self):
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "0"}), \
             patch.object(run_daily, "SeenJobsRegistry") as reg, \
             patch.object(run_daily, "run_daily_scrape") as scrape:
            summary = run_daily.run_pipeline()
        self.assertTrue(summary.get("autorun_disabled"))
        reg.assert_not_called()      # no state creation
        scrape.assert_not_called()   # no acquisition / network

    def test_enabled_run_pipeline_passes_guard(self):
        class _Sentinel(Exception):
            pass
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "1"}), \
             patch.object(run_daily, "SeenJobsRegistry", side_effect=_Sentinel):
            with self.assertRaises(_Sentinel):  # reached the first side effect
                run_daily.run_pipeline()


class MainChokepointTests(unittest.TestCase):
    def test_disabled_main_no_lock_no_pipeline(self):
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "0"}), \
             patch.object(run_daily, "PipelineRunLock") as lock, \
             patch.object(run_daily, "run_pipeline") as rp:
            rc = run_daily.main()
        self.assertEqual(rc, 0)
        lock.assert_not_called()
        rp.assert_not_called()

    def test_missing_flag_main_fails_safe(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_AUTORUN_ENABLED", None)
            with patch.object(run_daily, "run_pipeline") as rp:
                rc = run_daily.main()
        self.assertEqual(rc, 0)
        rp.assert_not_called()


class EntrypointGuardTests(unittest.TestCase):
    def test_disabled_entrypoint_single_message_no_main(self):
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "0"}), \
             patch.object(run_daily, "main") as m:
            with self.assertLogs("run_daily", level="WARNING") as cm:
                rc = run_daily.run_entrypoint()
        self.assertEqual(rc, 0)
        m.assert_not_called()
        msgs = [r for r in cm.output if "autorun disabled" in r.lower()]
        self.assertEqual(len(msgs), 1)  # exactly one, no duplicate

    def test_enabled_entrypoint_calls_main(self):
        with patch.dict(os.environ, {"PIPELINE_AUTORUN_ENABLED": "1"}), \
             patch.object(run_daily, "main", return_value=0) as m:
            rc = run_daily.run_entrypoint()
        self.assertEqual(rc, 0)
        m.assert_called_once()


class StartCommandContractTests(unittest.TestCase):
    """Real subprocess of the exact Railway Start Command."""
    def _run(self, autorun_env):
        env = dict(os.environ)
        env.update({"FANTASTIC_JOBS_ENABLED": "0", "ADZUNA_ENABLED": "0",
                    "ADZUNA_QUERY_PORTFOLIO_ENABLED": "0"})
        if autorun_env is None:
            env.pop("PIPELINE_AUTORUN_ENABLED", None)
        else:
            env["PIPELINE_AUTORUN_ENABLED"] = autorun_env
        proc = subprocess.run(
            [sys.executable, "-u", "run_daily.py"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
        )
        return proc

    def _assert_dormant(self, proc):
        out = (proc.stdout + proc.stderr).lower()
        self.assertEqual(proc.returncode, 0, out[-2000:])
        self.assertIn("autorun disabled", out)
        self.assertNotIn("step 1: scrape", out)      # run_pipeline never entered
        self.assertNotIn("searching jsearch", out)   # no acquisition/network
        self.assertNotIn("multi_source_acquisition", out)

    def test_start_command_disabled_is_dormant(self):
        self._assert_dormant(self._run("0"))

    def test_start_command_invalid_fails_safe(self):
        self._assert_dormant(self._run("invalid"))

    def test_start_command_missing_fails_safe(self):
        self._assert_dormant(self._run(None))


if __name__ == "__main__":
    unittest.main()
