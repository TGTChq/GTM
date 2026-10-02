"""Run ONLY the core offline gate in a child process without provider credentials.

Use a venv containing requirements-core-dev.txt. Embedded PostgreSQL must be
supported by the host. No network provider calls, deployment or git push here.
"""
from __future__ import annotations
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Keep only OS/runtime paths. In particular, never inherit API keys, a live/test
# database URL, PYTHONPATH hooks or application configuration from the developer shell.
OS_KEYS = {
    'PATH', 'HOME', 'USERPROFILE', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'TEMP', 'TMP', 'TMPDIR', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
    'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'LANG', 'LC_ALL',
}
env = {key: value for key, value in os.environ.items() if key.upper() in OS_KEYS}
env.update(PYTHONHASHSEED='0', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
command = [sys.executable, '-m', 'pytest', 'tests_core', '-q', '-p', 'ci_no_network',
           f'--junitxml={ROOT.parent / "core-takeover-results.xml"}']
if __name__ == '__main__':
    raise SystemExit(subprocess.run(command, cwd=ROOT, env=env, check=False).returncode)
