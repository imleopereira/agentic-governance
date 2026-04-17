"""
verify_citations.py — CI enforcement for the regulatory-and-citation-gate role.

STATUS: STUB. Tracked as v0.7 source-grep-tripwire work per `.agents/regulatory-and-citation-gate.md`.

Intent: scan public-facing files for the trigger regex (`NIST|EU AI Act|OWASP|SOC ?2|
ISO ?27001|Article \\d|ARS|CAISI|studies show|research shows|\\d+%`) and fail CI if
any match lacks a corresponding row in `.agent-outputs/research/primary-sources.md`.

Until implemented, enforcement is by manual researcher sign-off on every PR touching
the trigger regex. Do not mark this script as "done" in CI config until the logic below
is written and tested.
"""

from __future__ import annotations

import sys

TRIGGER_REGEX = (
    r"NIST|EU AI Act|OWASP|SOC ?2|ISO ?27001|Article \d|ARS|CAISI|"
    r"studies show|research shows|\d+%"
)

PUBLIC_SURFACES = (
    "README.md",
    "CHANGELOG.md",
    "docs/",
    "apex-consulting/",
)

PRIMARY_SOURCES_PATH = ".agent-outputs/research/primary-sources.md"


def main() -> int:
    # TODO(v0.7): implement scan + cross-check against primary-sources.md rows.
    # Until then, print the intent and exit 0 so CI does not block; manual
    # researcher sign-off is the authoritative gate.
    print(
        "verify_citations.py: STUB — manual researcher sign-off is authoritative "
        "until this script is implemented (tracked in v0.7 source-grep-tripwire)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
