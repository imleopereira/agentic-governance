"""pytest wrapper around scripts/test_no_hardcoded_creds.py.

Makes the credential-leak guard part of the normal test suite, so CI fails
before a hardcoded DB password can land on a branch.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = REPO_ROOT / "scripts" / "test_no_hardcoded_creds.py"


def test_no_hardcoded_credentials_in_scripts_or_tests() -> None:
    """Running the guard must exit 0 on a clean tree."""
    assert GUARD.is_file(), f"guard script missing: {GUARD}"
    result = subprocess.run(
        [sys.executable, str(GUARD)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "Credential-leak guard failed. stderr:\n" + result.stderr
    )
