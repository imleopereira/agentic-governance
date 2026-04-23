"""``codeatelier-governance recipe <template> <path>`` — project scaffolder.

Writes a ready-to-run agent project into ``<path>``. Pure Python, no
external deps, no network calls. The template files are shipped as
package data under :mod:`codeatelier_governance.recipes` and resolved
via ``importlib.resources`` so the same code path works for both the
wheel-installed case and editable checkouts.

Future recipes (``langgraph``, ``crewai``, …) plug in by adding a new
directory under ``codeatelier_governance/recipes/<name>/`` plus an
entry in :data:`codeatelier_governance.recipes.RECIPE_NAMES`.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import structlog

from ..recipes import RECIPE_NAMES, SCAFFOLD_FILES, recipe_resource_root

logger = structlog.get_logger(__name__)


def _target_has_content(path: Path) -> bool:
    """Return True if ``path`` exists and is non-empty.

    A brand-new, empty target directory (e.g. ``mkdir my-agent && cd``)
    is treated as "not yet scaffolded" so the user doesn't need to pass
    ``--force`` just because they ran ``mkdir`` first.
    """
    if not path.exists():
        return False
    if path.is_file():
        return True
    # Directory: non-empty means at least one entry (even a dotfile).
    return any(path.iterdir())


def _write_scaffold(template: str, target_dir: Path) -> list[Path]:
    """Materialise every ``SCAFFOLD_FILES`` entry into ``target_dir``.

    Returns the list of written file paths (absolute) for logging and
    tests. Does not prompt, does not substitute — templates are shipped
    verbatim; customisation is left to the user post-scaffold. This is
    intentional: every substitution token is a supply-chain surface.
    """
    root = recipe_resource_root(template)
    written: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in SCAFFOLD_FILES:
        src = root / src_name
        if not src.is_file():
            raise FileNotFoundError(
                f"recipe {template!r} is missing template file {src_name!r}; "
                "this is a packaging bug — please report."
            )
        # ``Traversable.read_text`` handles both wheel (zip) and editable
        # (filesystem) installs, so we don't need to materialise via
        # ``as_file``. UTF-8 is the only encoding templates ship in.
        content = src.read_text(encoding="utf-8")
        dst = target_dir / dst_name
        dst.write_text(content, encoding="utf-8")
        written.append(dst.resolve())
    return written


def run_recipe_command(template: str, path: str, *, force: bool = False) -> int:
    """Execute the ``recipe`` subcommand. Returns a shell exit code.

    * Exit 0 — scaffold written cleanly.
    * Exit 1 — target already exists and ``force`` is False, or writing failed.
    * Exit 2 — invalid template name.
    """
    if template not in RECIPE_NAMES:
        sys.stderr.write(
            f"Error: unknown recipe {template!r}. "
            f"Supported: {', '.join(RECIPE_NAMES)}.\n"
        )
        return 2

    raw_target = Path(path).expanduser()
    # Symlink refusal BEFORE .resolve(): resolve() dereferences, so running
    # --force against a symlinked directory would call shutil.rmtree() on
    # the resolved destination (e.g. `ln -s /etc/important my-agent` then
    # `recipe agt my-agent --force` would wipe /etc/important).
    if raw_target.is_symlink():
        sys.stderr.write(
            f"Error: {raw_target} is a symlink; refusing to --force "
            "(would clobber the link target).\n"
        )
        return 1
    target = raw_target.resolve()

    if _target_has_content(target):
        if not force:
            sys.stderr.write(
                f"Error: {target} already exists and is not empty. "
                "Re-run with --force to overwrite.\n"
            )
            return 1
        if target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    try:
        written = _write_scaffold(template, target)
    except (FileNotFoundError, OSError) as exc:
        sys.stderr.write(f"Error: failed to write scaffold ({exc}).\n")
        return 1

    logger.info(
        "cli.recipe.scaffolded",
        template=template,
        target=str(target),
        file_count=len(written),
    )
    sys.stdout.write(
        f"Scaffolded {len(written)} files into {target}:\n"
    )
    for f in written:
        sys.stdout.write(f"  {f.relative_to(target)}\n")
    sys.stdout.write(
        "\nNext:\n"
        f"  cd {target}\n"
        "  pip install -r requirements.txt\n"
        "  cp .env.example .env  &&  edit .env\n"
        "  python agent.py\n"
    )
    return 0
