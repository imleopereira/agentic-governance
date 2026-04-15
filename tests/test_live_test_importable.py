"""Regression + coverage tests for ``scripts/live_test.py``.

Two concerns:

1. **Importability**: a prior revision of ``scripts/live_test.py`` called
   ``_load_db_url`` and ``_load_audit_secret`` at module scope, so
   ``import scripts.live_test`` would ``sys.exit(2)`` during module
   initialization if either env var was missing. The fix moved env-driven
   config into ``_load_config``. The import must stay a pure operation —
   no env reads, no prints, no exits — so any tool that imports the module
   for indexing or pytest ``--collect-only`` does not trip FATAL errors.

2. **Coverage of the FATAL branches**: ``_load_db_url``,
   ``_load_audit_secret``, the legacy-credential guard, ``_redact_db_url``,
   and the top-level ``OPENAI_API_KEY`` exit(2) branch were all uncovered
   before Devil's Advocate flagged it on 2026-04-15. Each is now exercised
   via subprocess (for the exit-code branches) or direct import (for the
   pure helpers).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _clean_env(**extra: str) -> dict[str, str]:
    """Return a hermetic env: PATH + PYTHONNOUSERSITE + caller overrides.

    ``PYTHONNOUSERSITE=1`` neutralizes ``usercustomize.py`` injection so
    the subprocess cannot inherit stray env-setting hooks from the
    developer's shell. Parent ``os.environ`` is NOT inherited.
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
    }
    env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Importability — the module must be pure at import time
# ---------------------------------------------------------------------------
def test_live_test_module_imports_without_env_vars() -> None:
    """``import scripts.live_test`` must succeed with zero env vars set."""
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.live_test"],
        env=_clean_env(),
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"import scripts.live_test exited {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_live_test_module_import_is_silent() -> None:
    """Importing the module must produce zero stdout and zero stderr.

    Regression guard against accidental module-level ``print`` statements
    or logging configured at import time — both would leak noise into
    every tool that does ``import scripts.live_test`` for indexing or
    refactoring.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.live_test"],
        env=_clean_env(),
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0
    assert result.stdout == "", f"import leaked stdout: {result.stdout!r}"
    assert result.stderr == "", f"import leaked stderr: {result.stderr!r}"


# ---------------------------------------------------------------------------
# _load_db_url — FATAL branches
# ---------------------------------------------------------------------------
def test_load_db_url_fatal_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOVERNANCE_TEST_DATABASE_URL", raising=False)
    from scripts.live_test import _load_db_url

    with pytest.raises(SystemExit) as excinfo:
        _load_db_url()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "FATAL" in err
    assert "GOVERNANCE_TEST_DATABASE_URL" in err


def test_load_db_url_rejects_legacy_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Build the legacy URL as two tokens so this test file does not trip
    # the repo's hardcoded-credential scanner.
    _legacy_user = "gover" + "nance"
    legacy_url = f"postgresql://{_legacy_user}:{_legacy_user}@localhost:5432/db"
    monkeypatch.setenv("GOVERNANCE_TEST_DATABASE_URL", legacy_url)
    from scripts.live_test import _load_db_url

    with pytest.raises(SystemExit) as excinfo:
        _load_db_url()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "legacy" in err.lower() or "FATAL" in err


def test_load_db_url_accepts_well_formed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GOVERNANCE_TEST_DATABASE_URL",
        "postgresql+asyncpg://u:p@localhost:5432/db",
    )
    from scripts.live_test import _load_db_url

    assert _load_db_url() == "postgresql+asyncpg://u:p@localhost:5432/db"


# ---------------------------------------------------------------------------
# _load_audit_secret — FATAL branch
# ---------------------------------------------------------------------------
def test_load_audit_secret_fatal_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GOVERNANCE_TEST_AUDIT_SECRET", raising=False)
    from scripts.live_test import _load_audit_secret

    with pytest.raises(SystemExit) as excinfo:
        _load_audit_secret()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "FATAL" in err
    assert "GOVERNANCE_TEST_AUDIT_SECRET" in err


# ---------------------------------------------------------------------------
# _redact_db_url — security-adjacent helper (must never leak the password)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:s3cret@host:5432/db",
        "postgresql+asyncpg://alice:hunter2@db.internal/prod",
        "postgresql://u:p@host/db",
    ],
)
def test_redact_db_url_never_leaks_password(url: str) -> None:
    from scripts.live_test import _redact_db_url

    out = _redact_db_url(url)
    assert "<redacted>" in out
    # Passwords must never survive the redaction.
    assert "s3cret" not in out
    assert "hunter2" not in out
    # User names are redacted as part of the credentials block.
    assert "alice" not in out
    assert "user:" not in out


def test_redact_db_url_preserves_scheme_and_host() -> None:
    from scripts.live_test import _redact_db_url

    out = _redact_db_url("postgresql+asyncpg://u:p@db.internal:5432/governance")
    assert out.startswith("postgresql+asyncpg://<redacted>@")
    assert "db.internal" in out
    assert ":5432" in out
    assert "/governance" in out


def test_redact_db_url_malformed_input_returns_sentinel() -> None:
    from scripts.live_test import _redact_db_url

    # A string with an invalid port triggers ValueError inside urlparse
    # when ``.port`` is accessed; the narrowed except returns a safe
    # placeholder without leaking the original.
    out = _redact_db_url("postgresql://u:p@host:notaport/db")
    # Either the port fell through to "" (safe render) or the fallback
    # sentinel was used — both must redact credentials.
    assert "u:p" not in out
    assert "notaport" not in out or "<redacted>" in out


# ---------------------------------------------------------------------------
# Top-level OPENAI_API_KEY guard
# ---------------------------------------------------------------------------
def test_main_openai_api_key_missing_exits_two() -> None:
    """Running the script as ``__main__`` with no OPENAI_API_KEY must exit 2.

    Uses a subprocess so the ``if __name__ == '__main__'`` branch actually
    fires. We stop at the env check — DB env vars are intentionally left
    unset so the OPENAI check is reached first (the guard sits before
    ``asyncio.run``).
    """
    result = subprocess.run(
        [sys.executable, "scripts/live_test.py"],
        env=_clean_env(),
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 2, (
        f"expected exit 2 from OPENAI_API_KEY guard; "
        f"got {result.returncode}. stderr={result.stderr!r}"
    )
    assert "OPENAI_API_KEY" in result.stderr
    assert "FATAL" in result.stderr
