#!/usr/bin/env python3
"""Guard: fail if any script/test hardcodes a credential.

Blocks this class of leak at the source. Invoked both directly (pre-push hook)
and via ``tests/test_scripts_no_hardcoded_creds.py`` as part of ``pytest``.

Patterns flagged:
    POSTGRES_PASSWORD=postgres
    password=postgres  (case-insensitive, any separator)
    governance:governance  (in a DB URL context)
    audit_secret = "..."  (literal string assignment)

Exit 0 on clean scan. Exit 1 with a line-by-line report on any match.
"""
from __future__ import annotations

import pathlib
import re
import sys
from collections.abc import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SCAN_GLOBS: tuple[str, ...] = (
    "scripts/*.py",
    "scripts/*.sh",
    "tests/**/*.py",
)

# Each entry: (regex, human description).
PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"POSTGRES_PASSWORD\s*=\s*postgres\b"),
        "POSTGRES_PASSWORD=postgres literal",
    ),
    (
        re.compile(r"(?i)password\s*=\s*['\"]?postgres['\"]?"),
        "password=postgres literal",
    ),
    (
        re.compile(r"governance:governance@"),
        "governance:governance DB URL default",
    ),
    (
        re.compile(r"""audit_secret\s*=\s*['"][0-9a-fA-F]{8,}['"]"""),
        "audit_secret hex literal",
    ),
    (
        re.compile(r"""AUDIT_SECRET\s*=\s*['"][0-9a-fA-F]{8,}['"]"""),
        "AUDIT_SECRET hex literal",
    ),
)

# Allowlist: this guard script itself contains the patterns by design.
ALLOW_SUFFIXES: tuple[str, ...] = (
    "scripts/test_no_hardcoded_creds.py",
    "scripts/test_no_hardcoded_creds.sh",
    "tests/test_scripts_no_hardcoded_creds.py",
)


def _iter_files() -> Iterable[pathlib.Path]:
    seen: set[pathlib.Path] = set()
    for pattern in SCAN_GLOBS:
        for p in REPO_ROOT.glob(pattern):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def _is_allowlisted(path: pathlib.Path) -> bool:
    posix = path.as_posix()
    return any(posix.endswith(s) for s in ALLOW_SUFFIXES)


def scan() -> list[str]:
    offenses: list[str] = []
    for path in _iter_files():
        if _is_allowlisted(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            offenses.append(f"{path}: could not read ({exc})")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for regex, description in PATTERNS:
                if regex.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenses.append(
                        f"{rel}:{lineno}: {description}: {line.strip()[:160]}"
                    )
    return offenses


def main() -> int:
    offenses = scan()
    if offenses:
        sys.stderr.write("Hardcoded credentials detected:\n")
        for item in offenses:
            sys.stderr.write(f"  {item}\n")
        sys.stderr.write(
            "\nRemove literal credentials. Read them from env vars instead.\n"
        )
        return 1
    print("OK: no hardcoded credentials detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
