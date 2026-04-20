"""Project-scaffold recipes for ``codeatelier-governance recipe <template>``.

This package is separate from :mod:`codeatelier_governance.cli.recipes`
— the latter ships single-file starter templates for the older
``governance init agent-<kind>`` command, while this package ships
*multi-file* project scaffolds (directory with ``agent.py``,
``governance.py``, ``requirements.txt``, ``.env.example``,
``README.md``) that wrap real agent frameworks (Microsoft AGT today;
LangGraph and CrewAI planned).

Each recipe lives under ``<name>/`` as a collection of ``.tmpl``
package-data files. The CLI resolves them via
``importlib.resources.files`` so the same code path works for both the
wheel-installed case and editable checkouts.
"""
from __future__ import annotations

from importlib import resources

# Recipe names exposed through the CLI ``template`` choice. Kept as a
# tuple so callers can reuse it for argparse choices without a mutable
# ref leaking global state.
RECIPE_NAMES: tuple[str, ...] = ("agt",)

# Files a scaffolded project always contains. The CLI walks this list
# and writes ``<recipe>/<target_filename>`` for each entry. Keep the
# filenames stable — tests compile the resulting ``agent.py`` and
# customers paste ``requirements.txt`` into build pipelines.
SCAFFOLD_FILES: tuple[tuple[str, str], ...] = (
    ("agent.py.tmpl", "agent.py"),
    ("governance.py.tmpl", "governance.py"),
    ("requirements.txt.tmpl", "requirements.txt"),
    ("env.example.tmpl", ".env.example"),
    ("README.md.tmpl", "README.md"),
)


def recipe_resource_root(name: str) -> resources.abc.Traversable:
    """Return the ``importlib.resources`` handle for a recipe directory.

    Raises :class:`FileNotFoundError` if the recipe is unknown. The
    caller is expected to validate against :data:`RECIPE_NAMES` first
    and emit a user-facing error with the list of supported names.
    """
    if name not in RECIPE_NAMES:
        raise FileNotFoundError(f"unknown recipe: {name!r}")
    return resources.files(__name__) / name


__all__ = ["RECIPE_NAMES", "SCAFFOLD_FILES", "recipe_resource_root"]
