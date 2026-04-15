"""Validate that cron artifacts in this repo match their checked-in baseline.

F7 (v0.6 PRD, sub-item #2): cron artifacts are a classic source of
silent drift — someone edits a live crontab on a host, nobody updates
the repo, and a week later the "same" job does something slightly
different on three servers. This validator is the CI-side tree-diff
that catches drift.

Scope
-----
This tool walks the ``automation/`` directory at repo root looking for
cron-shaped artifacts (``*.cron``, ``crontab``, ``*.crontab``). For each
artifact it computes a canonical hash and compares it against the
corresponding entry in ``automation/.cron-baseline.json``. Any mismatch
fails CI with a diff.

**DX Consultant fix applied**: this validator runs in CI, NOT as a cron
job. Running a cron-validation job from cron is a circular dependency —
the validator would never catch drift introduced inside the validator's
own cron entry. GitHub Actions (``.github/workflows/test.yml``) is the
enforcement point.

Stub behaviour
--------------
As of v0.6 the ``automation/`` tree does not yet contain any cron
artifacts (the automation pipeline is a set of ``scripts/automation/*.sh``
helpers, not cron jobs checked into the repo). In that case this tool
prints a WARN line and exits 0. When cron artifacts are added later,
the same tool gains teeth automatically — no CI wiring change needed.

Exit codes
----------
* ``0`` — all artifacts match their baseline (or no artifacts found).
* ``1`` — drift detected, a baseline is missing, or the baseline file
  is malformed.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTOMATION_DIR = REPO_ROOT / "automation"
BASELINE_PATH = AUTOMATION_DIR / ".cron-baseline.json"

# File suffixes / names we treat as cron artifacts.
_CRON_SUFFIXES = (".cron", ".crontab")
_CRON_NAMES = ("crontab",)


def _find_cron_artifacts(root: Path) -> list[Path]:
    """Return sorted list of cron-shaped files under ``root``.

    Returns an empty list if ``root`` does not exist. The walk
    deliberately excludes ``.cron-baseline.json`` itself and any
    ``deprecated/`` subtree.
    """
    if not root.exists():
        return []
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "deprecated" in path.parts:
            continue
        if path.name == ".cron-baseline.json":
            continue
        if path.suffix in _CRON_SUFFIXES or path.name in _CRON_NAMES:
            out.append(path)
    return sorted(out)


def _hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of ``path``.

    We hash raw bytes (no normalisation). Cron files are
    whitespace-sensitive and a stray tab matters, so any difference
    must count as drift.
    """
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _load_baseline(path: Path) -> dict[str, str]:
    """Load the baseline mapping {relative_path: sha256}.

    Returns an empty dict if the baseline file does not exist. Raises
    ``SystemExit(1)`` with a clear message if the file exists but is
    malformed — a broken baseline must fail CI, not silently pass.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(
            f"ERROR: {path} is not valid JSON: {e}\n"
            "The cron baseline must be a JSON object "
            "mapping repo-relative paths to sha256 digests.\n"
        )
        raise SystemExit(1)
    if not isinstance(data, dict):
        sys.stderr.write(
            f"ERROR: {path} must contain a JSON object at the top level.\n"
        )
        raise SystemExit(1)
    return {str(k): str(v) for k, v in data.items()}


def main() -> int:
    """Entry point. Returns the exit code (0 = clean, 1 = drift)."""
    artifacts = _find_cron_artifacts(AUTOMATION_DIR)

    if not AUTOMATION_DIR.exists():
        sys.stdout.write(
            "WARN: automation/ directory does not exist. "
            "No cron artifacts to validate. "
            "This validator will gain teeth automatically when "
            "automation/ is populated.\n"
        )
        return 0

    if not artifacts:
        sys.stdout.write(
            "WARN: no cron artifacts found under automation/ "
            "(looked for *.cron, *.crontab, crontab). "
            "Validator is a no-op until cron artifacts land.\n"
        )
        return 0

    baseline = _load_baseline(BASELINE_PATH)

    drifted: list[tuple[str, str, str]] = []
    missing_from_baseline: list[str] = []
    for artifact in artifacts:
        rel = str(artifact.relative_to(REPO_ROOT))
        actual = _hash_file(artifact)
        expected = baseline.get(rel)
        if expected is None:
            missing_from_baseline.append(rel)
            continue
        if actual != expected:
            drifted.append((rel, expected, actual))

    stale_in_baseline = sorted(
        set(baseline.keys())
        - {str(a.relative_to(REPO_ROOT)) for a in artifacts}
    )

    if drifted or missing_from_baseline or stale_in_baseline:
        sys.stderr.write("CRON ARTIFACT DRIFT DETECTED\n")
        for rel, expected, actual in drifted:
            sys.stderr.write(
                f"  CHANGED: {rel}\n"
                f"    expected sha256: {expected}\n"
                f"    actual   sha256: {actual}\n"
            )
        for rel in missing_from_baseline:
            sys.stderr.write(
                f"  NEW (not in baseline): {rel}\n"
                "    Add a sha256 entry to "
                "automation/.cron-baseline.json to acknowledge.\n"
            )
        for rel in stale_in_baseline:
            sys.stderr.write(
                f"  REMOVED (still in baseline): {rel}\n"
                "    Delete the entry from "
                "automation/.cron-baseline.json.\n"
            )
        return 1

    sys.stdout.write(
        f"OK: {len(artifacts)} cron artifact(s) match baseline.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
