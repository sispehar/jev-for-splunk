"""jev_core must import and work without splunklib (CLI, tests, the offline rehearsal)."""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORE = os.path.join(REPO, "jev_for_splunk", "bin", "jev_core")


def test_no_splunklib_imports_in_core_sources():
    pattern = re.compile(r"^\s*(from|import)\s+splunklib", re.M)
    for path in glob.glob(os.path.join(CORE, "*.py")):
        with open(path, "r", encoding="utf-8") as handle:
            assert not pattern.search(handle.read()), path


def test_core_imports_with_splunklib_blocked():
    code = ("import sys; sys.modules['splunklib'] = None; sys.path.insert(0, %r); "
            "import jev_core.battery, jev_core.cache, jev_core.cli, jev_core.client, jev_core.config, "
            "jev_core.options, jev_core.primitive, jev_core.runner, jev_core.secrets, jev_core.state; print('ok')"
            % os.path.dirname(CORE))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
