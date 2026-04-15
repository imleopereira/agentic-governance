"""BLOCKER 1: enable_coverage must be opt-in.

CLAUDE.md invariant: "every module is opt-in via config, not code
changes." Prior to this fix the WrapperRegistry was instantiated
unconditionally in ``GovernanceSDK.__init__`` and then flushed in
``start()``, which meant v0.5 users upgrading to v0.6 without applying
the F9 migration would see a WARN log every startup. That behavior
change violated the invariant and DA flagged it as a BLOCKER.
"""
from __future__ import annotations

import logging
import secrets as _secrets

import pytest

from codeatelier_governance import GovernanceSDK
from codeatelier_governance.sdk import GovernanceConfig
from codeatelier_governance.coverage.registry import WrapperRegistry


FAKE_URL = "postgresql://fake:fake@localhost:1/fake"


def _sdk(**kwargs: object) -> GovernanceSDK:
    return GovernanceSDK(
        database_url=FAKE_URL,
        audit_secret=_secrets.token_bytes(32),
        **kwargs,  # type: ignore[arg-type]
    )


def test_default_disabled_no_registry_instantiated() -> None:
    sdk = _sdk()
    assert sdk._wrapper_registry is None


@pytest.mark.asyncio
async def test_default_disabled_no_warning_on_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    sdk = _sdk()
    try:
        # Engine-backed start() will still try to hit the fake DB for
        # various modules; we only care that the wrapper_registry.flush
        # code path is NOT reached and no "wrapper_registry" warning fires.
        try:
            await sdk.start()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await sdk.close()
        except Exception:  # noqa: BLE001
            pass
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "wrapper_registry" not in joined
    assert "wrapper coverage" not in joined.lower()


def test_enabled_instantiates_registry() -> None:
    sdk = GovernanceSDK(
        database_url=FAKE_URL,
        audit_secret=_secrets.token_bytes(32),
        enable_coverage=True,
    )
    assert isinstance(sdk._wrapper_registry, WrapperRegistry)


def test_disabled_wraps_still_function() -> None:
    """wrap_* helpers must be safe to call even when coverage is off."""
    sdk = _sdk()

    # Fake OpenAI-ish client so wrap_openai doesn't require real SDK.
    class _Completions:
        async def create(self, **_: object) -> None:  # pragma: no cover
            return None

    class _Chat:
        def __init__(self) -> None:
            self.completions = _Completions()

    class _Client:
        def __init__(self) -> None:
            self.chat = _Chat()

    from codeatelier_governance.integrations.openai_wrap import wrap_openai

    client = _Client()
    wrap_openai(client, sdk=sdk, agent_id="disabled-coverage-agent")
    # No registry → registration silently skipped, wrap still succeeds.
    assert getattr(client, "_governance_wrapped", False) is True
    assert sdk._wrapper_registry is None


@pytest.mark.asyncio
async def test_disabled_compliance_report_returns_registry_disabled_reason() -> None:
    """A report generator with no coverage collaborator reports the
    ``registry_disabled`` reason — verified via the default stub path."""
    from codeatelier_governance.compliance.report import ReportGenerator

    gen = ReportGenerator()
    pct, reason = await gen._coverage.compute(agent_id=None)
    assert pct is None
    assert reason == "registry_disabled"


def test_config_enable_coverage_default_is_false() -> None:
    cfg = GovernanceConfig()
    assert cfg.enable_coverage is False
