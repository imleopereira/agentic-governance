"""F7 sub-item #3: pre-commit TODO gate contract tests.

The bash hook at ``.githooks/pre-commit-todo-gate.sh`` blocks commits
that introduce partial-state IOUs (TODO(later), XXX, FIXME) without a
version tag. We test it by running it on ad-hoc temp files.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


HOOK = (
    Path(__file__).resolve().parent.parent
    / ".githooks"
    / "pre-commit-todo-gate.sh"
)


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_hook_exists_and_is_readable() -> None:
    assert HOOK.exists(), HOOK


def test_hook_blocks_fixme_in_staged_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def foo():\n    # FIXME: broken\n    pass\n")
    proc = _run([str(bad)])
    assert proc.returncode != 0, (
        f"hook should block FIXME, got rc={proc.returncode}\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_hook_allows_clean_file(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("def foo():\n    return 1\n")
    proc = _run([str(good)])
    assert proc.returncode == 0, (
        f"hook should allow clean file, got rc={proc.returncode}\n"
        f"stderr={proc.stderr}"
    )


def test_hook_allows_version_tagged_todo(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text("# TODO(v0.6.1): wire up after F11 lands\nx = 1\n")
    proc = _run([str(good)])
    assert proc.returncode == 0, (
        f"version-tagged TODO must be allowed, got rc={proc.returncode}\n"
        f"stderr={proc.stderr}"
    )
