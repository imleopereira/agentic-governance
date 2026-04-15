"""Regression: ``scripts.live_test`` must be importable with no env vars set.

A prior revision of ``scripts/live_test.py`` called ``_load_db_url`` and
``_load_audit_secret`` at module scope, so ``import scripts.live_test``
would ``sys.exit(2)`` during module initialization if either env var was
missing. This broke any tool that imported the module for indexing,
collection (pytest --collect-only), or refactoring.

The fix: all env-driven config lives inside ``_load_config``, which is
called only from ``run_all_tests``. Module import is a pure operation.

This test runs in a clean child process with NOTHING inherited from the
outer shell except ``PATH`` (so Python can find itself). If either env
var ever creeps back to module scope, this test fails loud.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_live_test_module_imports_without_env_vars() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.live_test"],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert result.returncode == 0, (
        f"import scripts.live_test exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
