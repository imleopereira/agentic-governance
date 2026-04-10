"""Tests for F3: emit_audit.py metadata size cap."""
from __future__ import annotations

import subprocess
import sys


def test_oversized_metadata_rejected() -> None:
    """Metadata > 64KB should be rejected with NULL UUID."""
    big_metadata = '{"key": "' + "x" * 70000 + '"}'
    result = subprocess.run(
        [
            sys.executable,
            "src/codeatelier_governance/cli/emit_audit.py",
            "--kind", "test.event",
            "--agent-id", "test-agent",
            "--session-id", "00000000-0000-0000-0000-000000000001",
            "--metadata", big_metadata,
        ],
        capture_output=True,
        text=True,
        env={"APEX_CONTENT_AUDIT_ENABLED": "true", "PATH": ""},
    )
    assert "00000000-0000-0000-0000-000000000000" in result.stdout
    assert "64KB" in result.stderr or "limit" in result.stderr.lower()


def test_normal_metadata_accepted() -> None:
    """Small metadata should not be rejected by the size check."""
    result = subprocess.run(
        [
            sys.executable,
            "src/codeatelier_governance/cli/emit_audit.py",
            "--kind", "test.event",
            "--agent-id", "test-agent",
            "--session-id", "00000000-0000-0000-0000-000000000001",
            "--metadata", '{"key": "value"}',
        ],
        capture_output=True,
        text=True,
        env={"APEX_CONTENT_AUDIT_ENABLED": "true", "PATH": ""},
    )
    # Should not fail due to size (may fail for other reasons like no DB)
    assert "64KB" not in result.stderr and "limit" not in result.stderr.lower()
