"""The validation launcher strips real credentials and guards child processes."""
import subprocess
import sys

from rebuild import run_offline_tests


def test_launcher_does_not_inherit_secrets_or_production_database(monkeypatch):
    for key in ("APOLLO_API_KEY", "ANTHROPIC_API_KEY", "TGTC_DATABASE_URL", "TGTC_TEST_DATABASE_URL", "PYTEST_ADDOPTS"):
        monkeypatch.setenv(key, "must-not-enter-child")
    real_run = subprocess.run
    calls = []
    def inspect(argv, *, cwd, env, check):
        calls.append(argv)
        assert not any(value == "must-not-enter-child" for value in env.values())
        assert env["PYTHON_DOTENV_DISABLED"] == "1"
        # Synthetic audit event: no DNS lookup or socket is opened by this test.
        code = ("import sys,ci_no_network; assert ci_no_network._AUDIT_ACTIVE\n"
                "try: sys.audit('socket.connect', None, ('192.0.2.1',443))\n"
                "except ci_no_network.NetworkUseInTests: pass\n"
                "else: raise AssertionError('child guard missing')\n")
        return real_run([sys.executable, "-c", code], cwd=cwd, env=env, check=check)
    monkeypatch.setattr(subprocess, "run", inspect)
    assert run_offline_tests.main(["tests_core", "-q"]) == 0
    assert calls[0][1:5] == ["-m", "pytest", "-p", "ci_no_network"]
