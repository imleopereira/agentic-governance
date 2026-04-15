"""POLISH 1: no bare ``assert`` on the chain verification security path.

``python -O`` turns bare ``assert`` into a no-op, so using asserts to
guard security invariants (e.g. "key material must be resolved here")
is a footgun: under -O the invariant silently fails.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_chain_module_has_no_bare_asserts_in_security_paths() -> None:
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "codeatelier_governance"
        / "audit"
        / "chain.py"
    ).read_text()

    # Scope: only the F6 Track A and Track B sections. Lines outside
    # those sections are unrelated and not part of this audit.
    start_b = src.index("=== F6 Track B:")
    end_a = src.index("=== End F6 Track A")
    track_section = src[start_b:end_a]

    # Ignore doc-strings in the audit. Just check concrete lines.
    offending: list[str] = []
    for i, line in enumerate(track_section.splitlines(), 1):
        if re.match(r"^\s*assert\s", line):
            offending.append(f"{i}: {line}")
    assert not offending, (
        "Bare `assert` on security-critical chain verification path "
        "(no-op under python -O): " + "; ".join(offending)
    )
