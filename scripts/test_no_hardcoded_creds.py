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

import fnmatch
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterable

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

SCAN_GLOBS: tuple[str, ...] = (
    "scripts/**/*.py",
    "scripts/**/*.sh",
    "tests/**/*.py",
    "src/**/*.py",
    "README.md",
    "pyproject.toml",
    "**/*.yaml",
    "**/*.toml",
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
    # Generic URL with embedded userinfo (e.g. postgres://user:password@host).
    # Catches hardcoded credentials inside any protocol URL, not just the
    # governance:governance default. Anything matching ``://u:p@`` in any
    # scanned file trips the guard, UNLESS both the user and password are
    # obvious placeholders (``user``, ``pass``, ``password``, ``fake``,
    # ``x``, ``<user>``, ``<pass>``, ``...``) — those belong in docstrings
    # and READMEs. The check happens in ``_line_trips_userinfo_pattern``
    # below because a pure regex cannot express "reject only real-looking
    # credentials".
    (
        re.compile(r"://(?P<user>[^:/\s]+):(?P<pw>[^@/\s]+)@"),
        "URL with embedded userinfo credentials",
    ),
)

# Lowercase tokens that mean "this is a placeholder, not a real credential".
# If BOTH halves of a userinfo pair are in this set (or look like a
# ``<bracketed>`` / ``${var}`` / all-punct placeholder), we don't flag it.
_PLACEHOLDER_USERINFO: frozenset[str] = frozenset(
    {
        "user",
        "username",
        "pass",
        "password",
        "passwd",
        "fake",
        "x",
        "u",
        "p",
        "admin",
        "me",
        "you",
        "test",
        "example",
        "changeme",
        "secret",
        "s3cret",
        "...",
    }
)


def _is_placeholder(token: str) -> bool:
    t = token.strip().lower()
    if not t:
        return True
    if t in _PLACEHOLDER_USERINFO:
        return True
    # ``<user>`` / ``<pass>`` / ``${VAR}`` / ``%VAR%`` — templating syntax.
    if t.startswith(("<", "${", "%")) and t.endswith((">", "}", "%")):
        return True
    return False

# Allowlist: this guard script itself contains the patterns by design.
ALLOW_SUFFIXES: tuple[str, ...] = (
    "scripts/test_no_hardcoded_creds.py",
    "scripts/test_no_hardcoded_creds.sh",
    "tests/test_scripts_no_hardcoded_creds.py",
)


def _git_tracked_files() -> list[pathlib.Path]:
    """Return every file tracked by git at the repo root.

    Using git as the source of truth means gitignored agent-scratch files
    (under ``scripts/automation/``, etc.) are never scanned. If git isn't
    available we fall back to a filesystem walk.

    **Scope limitation:** only *tracked* files are scanned. Files in the
    working tree that have not yet been ``git add``'d are invisible to
    this guard. If you are adding a new file with a credential pattern,
    either run the guard *after* staging (``git add FILE && python
    scripts/test_no_hardcoded_creds.py``) or rely on the pytest wrapper
    ``tests/test_scripts_no_hardcoded_creds.py`` which runs on every
    ``pytest`` invocation once the file is staged. The intent is that
    commit hooks and CI re-run the guard after ``git add``.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [p for p in REPO_ROOT.rglob("*") if p.is_file()]
    paths: list[pathlib.Path] = []
    for chunk in out.stdout.split(b"\x00"):
        if not chunk:
            continue
        paths.append(REPO_ROOT / chunk.decode("utf-8", errors="replace"))
    return paths


def _iter_files() -> Iterable[pathlib.Path]:
    seen: set[pathlib.Path] = set()
    tracked = _git_tracked_files()
    for path in tracked:
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in SCAN_GLOBS):
            if path not in seen:
                seen.add(path)
                yield path


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
                match = regex.search(line)
                if not match:
                    continue
                # Placeholder exemption for the URL-userinfo pattern.
                if "user" in match.groupdict() and "pw" in match.groupdict():
                    if _is_placeholder(match.group("user")) and _is_placeholder(
                        match.group("pw")
                    ):
                        continue
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
