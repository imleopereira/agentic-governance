"""``governance init agent-<kind>`` — scaffold a governed-agent starter file.

Writes a single ruff-clean, mypy-strict, ``--smoke``-passable Python file to
the caller's cwd. Uses three curated recipes (customer-support,
data-enrichment, internal-research) — no free-form template authoring.
The CLI is additive: existing ``governance`` subcommands are untouched.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import structlog

from .recipes import RECIPE_KINDS, template_path

logger = structlog.get_logger(__name__)

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_KIND_PREFIX = "agent-"


def _resolve_kind(raw: str) -> str:
    """Strip the ``agent-`` prefix and validate against :data:`RECIPE_KINDS`."""
    if not raw.startswith(_KIND_PREFIX):
        raise ValueError(
            f"unknown recipe {raw!r}. Expected 'agent-<kind>' where "
            f"<kind> is one of: {', '.join(RECIPE_KINDS)}."
        )
    kind = raw[len(_KIND_PREFIX):]
    if kind not in RECIPE_KINDS:
        raise ValueError(
            f"unknown recipe kind {kind!r}. Supported: {', '.join(RECIPE_KINDS)}."
        )
    return kind


def _prompt(label: str, default: str, *, stream: Iterable[str] | None = None) -> str:
    """Prompt stdin with a default fallback. ``stream`` drives non-interactive tests."""
    if stream is not None:
        # Test/scripted path: pull the next canned line; empty falls back to default.
        raw = next(iter(stream), "")
        return raw.strip() or default
    sys.stdout.write(f"{label} [{default}]: ")
    sys.stdout.flush()
    line = sys.stdin.readline().rstrip("\n")
    return line.strip() or default


def _safe_target_path(raw: str, agent_id: str) -> Path:
    """Resolve ``raw`` relative to cwd, rejecting path traversal + absolute escapes."""
    base = Path.cwd().resolve()
    candidate = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if not str(candidate).startswith(str(base)):
        raise ValueError(
            f"refused: output path {raw!r} escapes the current working directory."
        )
    if candidate.is_dir():
        candidate = candidate / f"{agent_id.replace('-', '_')}.py"
    return candidate


def _render(kind: str, *, agent_id: str, description: str) -> str:
    """Load the recipe template and substitute the 3 context fields."""
    tmpl = template_path(kind).read_text(encoding="utf-8")
    return tmpl.format(
        agent_id=agent_id,
        description=description.replace("\n", " ").strip(),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def run_init(
    kind_arg: str,
    *,
    force: bool = False,
    prompts: Iterable[str] | None = None,
) -> int:
    """Execute ``governance init agent-<kind>``. Returns a shell exit code.

    ``prompts`` lets callers (and tests) feed canned answers instead of
    reading ``sys.stdin``. Three prompts: agent id, output path, description.
    """
    try:
        kind = _resolve_kind(kind_arg)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    stream = iter(prompts) if prompts is not None else None
    default_id = kind.replace("-", "_") + "_agent"
    agent_id = _prompt("Agent id", default_id, stream=stream)
    if not _AGENT_ID_RE.match(agent_id):
        sys.stderr.write(
            "Error: agent id must match ^[a-z0-9][a-z0-9-]{1,63}$ "
            "(lowercase alnum, hyphen, 2-64 chars).\n"
        )
        return 2

    default_out = f"./{agent_id.replace('-', '_')}.py"
    out_raw = _prompt("Output path", default_out, stream=stream)
    try:
        target = _safe_target_path(out_raw, agent_id)
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    description = _prompt("Brief description", f"{kind.replace('-', ' ')} agent", stream=stream)

    rendered = _render(kind, agent_id=agent_id, description=description)

    if target.exists() and not force:
        sys.stderr.write(
            f"Error: {target} already exists. Re-run with --force to overwrite.\n"
        )
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL when not forcing; truncate-create when forcing. This closes
    # the TOCTOU race between the exists() check above and the open() here.
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(target, flags, 0o644)
    except FileExistsError:
        sys.stderr.write(
            f"Error: {target} was created concurrently. Re-run with --force.\n"
        )
        return 1
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(rendered)

    logger.info(
        "cli.init.executed",
        recipe_kind=kind,
        agent_id=agent_id,
        target=str(target),
    )
    sys.stdout.write(
        f"Wrote {target}\n"
        f"Next: python {target.name} --smoke  (should exit 0)\n"
    )
    return 0


def register_subparser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Wire ``init`` into the top-level argparse dispatcher."""
    p = subparsers.add_parser(
        "init",
        help="Scaffold a governed-agent starter file (agent-customer-support, etc.)",
    )
    p.add_argument(
        "recipe",
        help=f"Recipe name. One of: {', '.join('agent-' + k for k in RECIPE_KINDS)}",
    )
    p.add_argument(
        "--force", action="store_true", default=False,
        help="Overwrite the target file if it already exists.",
    )
