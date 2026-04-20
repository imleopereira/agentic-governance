"""Tests for the v0.7.2 multi-file recipe scaffolder.

Covers the ``codeatelier-governance recipe agt <path>`` command. Every
expected file is written, ``--force`` overwrites cleanly, the help
output is useful, bare re-runs refuse to clobber, and the generated
``agent.py`` compiles with ``compile()``.
"""
from __future__ import annotations

import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from codeatelier_governance.cli.commands import main as cli_main
from codeatelier_governance.cli.recipe import run_recipe_command
from codeatelier_governance.recipes import SCAFFOLD_FILES

_AGT_EXPECTED_FILES = tuple(dst for _src, dst in SCAFFOLD_FILES)


def test_scaffold_creates_all_expected_files(tmp_path: Path) -> None:
    """recipe agt <path> writes every scaffold file into a fresh directory."""
    target = tmp_path / "my-agent"
    rc = run_recipe_command("agt", str(target), force=False)
    assert rc == 0, "scaffold should succeed on a fresh path"
    assert target.is_dir()
    for filename in _AGT_EXPECTED_FILES:
        path = target / filename
        assert path.is_file(), f"expected {filename} in scaffold"
        assert path.read_text().strip(), f"{filename} is empty"


def test_generated_agent_py_compiles(tmp_path: Path) -> None:
    """The scaffolded ``agent.py`` must be importable Python — customers run it."""
    target = tmp_path / "compile-check"
    assert run_recipe_command("agt", str(target)) == 0
    agent_py = target / "agent.py"
    # ``py_compile.compile`` raises PyCompileError on any SyntaxError.
    py_compile.compile(str(agent_py), doraise=True)
    # Also check the companion governance.py for good measure — it's
    # the module ``agent.py`` imports, so a broken file there would
    # surface as ImportError at customer runtime.
    py_compile.compile(str(target / "governance.py"), doraise=True)


def test_target_exists_without_force_refuses(tmp_path: Path) -> None:
    """A populated target directory must require --force to overwrite."""
    target = tmp_path / "taken"
    target.mkdir()
    (target / "user-file.txt").write_text("important user data\n")

    rc = run_recipe_command("agt", str(target), force=False)
    assert rc == 1, "scaffold must refuse non-empty existing target"
    # The user's file must be preserved — no silent clobber.
    assert (target / "user-file.txt").read_text() == "important user data\n"


def test_force_overwrites_existing_directory(tmp_path: Path) -> None:
    """--force replaces the target wholesale, dropping stale files."""
    target = tmp_path / "replaceme"
    target.mkdir()
    (target / "stale.txt").write_text("from a previous scaffold\n")

    rc = run_recipe_command("agt", str(target), force=True)
    assert rc == 0
    # Every expected file is present after force-overwrite...
    for filename in _AGT_EXPECTED_FILES:
        assert (target / filename).is_file()
    # ...and the stale pre-existing file is gone.
    assert not (target / "stale.txt").exists()


def test_cli_help_prints_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``codeatelier-governance --help`` surfaces the recipe subcommand."""
    with pytest.raises(SystemExit) as exc:
        cli_main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "recipe" in out, "top-level help must list the recipe subcommand"


def test_recipe_help_lists_templates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``codeatelier-governance recipe --help`` shows the ``agt`` template."""
    with pytest.raises(SystemExit) as exc:
        cli_main(["recipe", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "agt" in out
    assert "--force" in out


def test_unknown_template_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unsupported recipe name returns non-zero with a helpful message."""
    rc = run_recipe_command("does-not-exist", str(tmp_path / "out"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown recipe" in err
    assert "agt" in err, "error must list supported recipes"


def test_python_m_invocation_creates_scaffold(tmp_path: Path) -> None:
    """``python -m codeatelier_governance.cli recipe agt <path>`` end-to-end.

    Uses a subprocess so we exercise the real entry point customers
    hit — ``[project.scripts]`` resolves to the same ``main()`` the
    ``python -m`` form runs.
    """
    target = tmp_path / "subproc-agt"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "codeatelier_governance.cli",
            "recipe",
            "agt",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    assert target.is_dir()
    assert (target / "agent.py").is_file()
    assert (target / "requirements.txt").read_text().startswith(
        "code-atelier-governance"
    )
