"""Tests for ``governance init agent-<kind>`` (F3, v0.7).

Every generated file must:
* parse as valid Python (syntactically),
* exit 0 under ``--smoke`` with no env vars set,
* import the governance SDK from the installed package,
* include the caller's description verbatim.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codeatelier_governance.cli.init import run_init
from codeatelier_governance.cli.recipes import RECIPE_KINDS


@pytest.mark.parametrize("kind", RECIPE_KINDS)
def test_each_recipe_generates_valid_python(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every canonical recipe must generate a file that parses cleanly."""
    monkeypatch.chdir(tmp_path)
    agent_id = f"test-{kind}"
    prompts = [agent_id, f"./{kind.replace('-', '_')}.py", "scaffold smoke test"]
    rc = run_init(f"agent-{kind}", prompts=prompts)
    assert rc == 0
    target = tmp_path / f"{kind.replace('-', '_')}.py"
    assert target.exists()
    source = target.read_text()
    # ast.parse raises SyntaxError if the generated file is broken.
    ast.parse(source)
    # Every template must reference the SDK top-level import so users
    # find the 5-lines-to-enforcement story in their own file.
    assert "from codeatelier_governance import" in source


@pytest.mark.parametrize("kind", RECIPE_KINDS)
def test_generated_file_smoke_exits_zero(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``python <generated> --smoke`` must exit 0 without a database."""
    monkeypatch.chdir(tmp_path)
    agent_id = f"smoke-{kind}"
    prompts = [agent_id, f"./{kind.replace('-', '_')}.py", "smoke run"]
    assert run_init(f"agent-{kind}", prompts=prompts) == 0

    target = tmp_path / f"{kind.replace('-', '_')}.py"
    # Scrub any ambient GOVERNANCE_DATABASE_URL so we prove the --smoke
    # path doesn't touch Postgres. Preserve PATH so sys.executable works.
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    result = subprocess.run(
        [sys.executable, str(target), "--smoke"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    assert "smoke OK" in result.stdout


def test_unknown_kind_errors_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = run_init("agent-does-not-exist")
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown recipe kind" in err
    # The error message should list the supported kinds so the fix is obvious.
    for kind in RECIPE_KINDS:
        assert kind in err


def test_missing_agent_prefix_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = run_init("customer-support")  # missing "agent-" prefix
    assert rc == 2
    assert "agent-<kind>" in capsys.readouterr().err


def test_existing_target_requires_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-existing file is preserved unless the caller passes --force."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "customer_support.py"
    target.write_text("# pre-existing user edits\n")
    prompts = ["test-cs", "./customer_support.py", "hello"]
    rc = run_init("agent-customer-support", prompts=list(prompts))
    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    # Original contents untouched.
    assert target.read_text() == "# pre-existing user edits\n"

    rc_force = run_init("agent-customer-support", force=True, prompts=list(prompts))
    assert rc_force == 0
    assert "pre-existing" not in target.read_text()


def test_description_shelled_into_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The description prompt answer must land verbatim in the generated file."""
    monkeypatch.chdir(tmp_path)
    description = "Handles refund disputes and escalations to human reviewers."
    prompts = ["refund-bot", "./refund_bot.py", description]
    assert run_init("agent-customer-support", prompts=prompts) == 0
    text = (tmp_path / "refund_bot.py").read_text()
    assert description in text
    # AGENT_ID constant mirrors the first prompt answer verbatim.
    assert 'AGENT_ID = "refund-bot"' in text


def test_invalid_agent_id_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    # Uppercase is a regex violation — also covers the "UPPER" path-traversal
    # lookalike where the user pastes a bad id from their IDE clipboard.
    prompts = ["BadAgent", "./x.py", "desc"]
    assert run_init("agent-customer-support", prompts=prompts) == 2
    assert "agent id must match" in capsys.readouterr().err


def test_path_traversal_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``../`` escape is refused so the CLI can't clobber files outside cwd."""
    monkeypatch.chdir(tmp_path)
    prompts = ["good-agent", "../escape.py", "desc"]
    assert run_init("agent-customer-support", prompts=prompts) == 2
    assert "escapes" in capsys.readouterr().err


def test_parser_wires_init_subcommand() -> None:
    """The top-level argparse dispatcher must expose ``init``."""
    from codeatelier_governance.cli.commands import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["init", "agent-customer-support"])
    assert args.command == "init"
    assert args.recipe == "agent-customer-support"
    assert args.force is False

    args_force = parser.parse_args(["init", "agent-data-enrichment", "--force"])
    assert args_force.force is True
