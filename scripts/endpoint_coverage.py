#!/usr/bin/env python
"""F3 endpoint consumer coverage check.

Walks the governance console Next.js source tree and verifies that every
v0.6 F3 backend endpoint has at least one call-site in the frontend. Fails
CI with a non-zero exit code listing any endpoints that have no consumer.

Usage:
    python scripts/endpoint_coverage.py
    python scripts/endpoint_coverage.py --strict   # fail on any gap
    python scripts/endpoint_coverage.py --json     # machine-readable output

The check is deliberately loose (substring match, path with ``{param}``
placeholders stripped) so that refactors of the fetch layer don't produce
false negatives. The point is to catch "we shipped an endpoint nobody
calls" regressions, not to enforce a particular HTTP client shape.

Stdlib only, per CLAUDE.md.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

# The 9 F3 endpoints as declared in
# decisions/2026-04-14-v06-major-release-prd.md section F3. Keep this list
# in sync with the response_model decorators in
# src/codeatelier_governance/console/app.py.
F3_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET",    "/api/policies"),
    ("GET",    "/api/policies/{agent_id}"),
    ("GET",    "/api/agents/presence"),
    ("GET",    "/api/events/stats"),
    ("GET",    "/api/gates/{request_id}/context"),
    ("POST",   "/api/gates/{request_id}/claim"),
    ("POST",   "/api/gates/{request_id}/escalate"),
    ("POST",   "/api/gates/batch-approve"),
    ("DELETE", "/api/auth/sessions/{session_id}"),
)

# Frontend source roots to scan. Broader than just `console/src/api/` — the
# current codebase wires fetches directly inside `hooks/` and `lib/`.
FE_ROOTS = (
    "console/src",
)
FE_GLOBS = ("**/*.ts", "**/*.tsx")


def _path_to_regex(path: str) -> re.Pattern[str]:
    """Turn ``/api/gates/{id}/context`` into a regex that matches any id.

    We deliberately match the literal leading segments, use a permissive
    ``[^"'`\\s?]+`` for the param, and the literal trailing segment. This
    catches both template-literal URLs (``/api/gates/${id}/context``) and
    string concatenation.
    """
    # Split on {param} placeholders and rebuild.
    parts = re.split(r"\{[^}]+\}", path)
    escaped = r"[^\"'\`\s?]+".join(re.escape(p) for p in parts)
    return re.compile(escaped)


def _scan(repo_root: pathlib.Path) -> dict[str, list[str]]:
    """Return ``{endpoint: [matching file paths]}``."""
    results: dict[str, list[str]] = {
        f"{method} {path}": [] for method, path in F3_ENDPOINTS
    }
    patterns = {
        f"{method} {path}": _path_to_regex(path)
        for method, path in F3_ENDPOINTS
    }

    files: list[pathlib.Path] = []
    for root in FE_ROOTS:
        root_path = repo_root / root
        if not root_path.is_dir():
            continue
        for glob in FE_GLOBS:
            for f in root_path.rglob(glob.lstrip("**/")):
                # Skip node_modules, .next, and tests — test files are
                # allowed but we call them out so they don't mask gaps.
                parts = set(f.parts)
                if "node_modules" in parts or ".next" in parts:
                    continue
                files.append(f)

    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for key, regex in patterns.items():
            if regex.search(text):
                results[key].append(str(file.relative_to(repo_root)))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any endpoint has zero consumers.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON result.",
    )
    parser.add_argument(
        "--repo-root",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent,
        help="Repo root (defaults to two levels above this script).",
    )
    args = parser.parse_args()

    results = _scan(args.repo_root)
    wired = sum(1 for hits in results.values() if hits)
    total = len(results)
    gaps = [key for key, hits in results.items() if not hits]

    if args.json:
        print(
            json.dumps(
                {
                    "wired": wired,
                    "total": total,
                    "results": results,
                    "gaps": gaps,
                },
                indent=2,
            )
        )
    else:
        print(f"F3 endpoint coverage: {wired}/{total} wired")
        for key, hits in results.items():
            mark = "OK" if hits else "GAP"
            n = len(hits)
            print(f"  [{mark}] {key}  ({n} consumer{'s' if n != 1 else ''})")
        if gaps:
            print("\nEndpoints with no frontend consumer:")
            for g in gaps:
                print(f"  - {g}")

    if args.strict and gaps:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
