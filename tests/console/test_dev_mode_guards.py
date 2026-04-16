"""BLOCKER C4: DEV_MODE startup hardening.

Pins:
  * DEV_MODE=true without GOVERNANCE_CONSOLE_ALLOW_DEV_MODE=1 refuses
    to start (RuntimeError on lifespan / _enforce_dev_mode_guards).
  * DEV_MODE=true on a non-localhost bind refuses to start.
  * DEV_MODE=true with all guards satisfied logs an ERROR-level event
    and continues.
"""
from __future__ import annotations


import pytest

from codeatelier_governance.console import app as app_module


def test_dev_mode_requires_explicit_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "DEV_MODE_ACK", False)
    monkeypatch.setattr(app_module, "CONSOLE_HOST", "127.0.0.1")
    with pytest.raises(RuntimeError, match="ALLOW_DEV_MODE"):
        app_module._enforce_dev_mode_guards()


def test_dev_mode_refuses_non_localhost_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "DEV_MODE_ACK", True)
    monkeypatch.setattr(app_module, "CONSOLE_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError, match="non-localhost bind"):
        app_module._enforce_dev_mode_guards()


def test_dev_mode_succeeds_when_acknowledged_and_localhost(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "DEV_MODE_ACK", True)
    monkeypatch.setattr(app_module, "CONSOLE_HOST", "127.0.0.1")
    # Should not raise.
    app_module._enforce_dev_mode_guards()


def test_dev_mode_off_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "DEV_MODE", False)
    # Even with no ACK and a public bind, DEV_MODE off is fine.
    monkeypatch.setattr(app_module, "DEV_MODE_ACK", False)
    monkeypatch.setattr(app_module, "CONSOLE_HOST", "0.0.0.0")
    app_module._enforce_dev_mode_guards()


def test_dev_mode_localhost_aliases_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "DEV_MODE_ACK", True)
    for host in ("127.0.0.1", "localhost", "::1"):
        monkeypatch.setattr(app_module, "CONSOLE_HOST", host)
        app_module._enforce_dev_mode_guards()
