"""v0.6.2 followup upgrade-breaker: SDK-level opt-out for strict_unknown_models.

Pre-followup, ``GovernanceSDK`` did not forward ``strict_unknown_models``
or ``unknown_model_fallback_usd_per_million`` to ``CostModule``. A
customer who hit the new v0.6.2 ``UnknownModelError`` had no
configuration-level opt-out — they had to subclass CostModule. That
violates the "every module is opt-in via config, not code changes"
invariant in CLAUDE.md.

Fix: ``GovernanceConfig.cost_strict_unknown_models`` (default ``True``,
preserves v0.6.2 P0 fix) and
``GovernanceConfig.cost_unknown_model_fallback_usd_per_million``
(default ``None``). Both forward into ``CostModule``. Both can be set
via env vars ``GOVERNANCE_COST_STRICT_UNKNOWN_MODELS`` and
``GOVERNANCE_COST_UNKNOWN_MODEL_FALLBACK_USD_PER_MILLION``.

Precedence: explicit kwarg > env var > default.
"""
from __future__ import annotations

import secrets

import pytest

from codeatelier_governance.cost.errors import UnknownModelError
from codeatelier_governance.sdk import GovernanceConfig, GovernanceSDK

# SDK's audit secret strength check rejects low-entropy placeholders
# like ``b"x" * 32``. Use a real random secret for every test.
_STRONG_SECRET = secrets.token_bytes(32)


def test_config_defaults_preserve_v062_strict_behavior() -> None:
    """Default config keeps strict=True so upgrades don't silently regress."""
    cfg = GovernanceConfig(database_url="postgresql://localhost/test")
    assert cfg.cost_strict_unknown_models is True
    assert cfg.cost_unknown_model_fallback_usd_per_million is None


def test_kwarg_disables_strict_and_cost_module_honors_it() -> None:
    """Passing cost_strict_unknown_models=False wires through to CostModule."""
    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
        cost_strict_unknown_models=False,
        cost_unknown_model_fallback_usd_per_million=2.5,
    )
    assert sdk.cost._strict_unknown_models is False
    assert sdk.cost._unknown_model_fallback_usd_per_million == 2.5


def test_cost_module_strict_default_still_raises() -> None:
    """With defaults, an unknown model still raises at CostModule level.

    This locks the P0 fix: flipping the SDK default to False would
    silently re-open the silent-zero budget-bypass vector.
    """
    from codeatelier_governance.cost.pricing import estimate_cost

    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
    )
    strict_from_cost_mod = sdk.cost._strict_unknown_models
    assert strict_from_cost_mod is True

    with pytest.raises(UnknownModelError):
        estimate_cost(
            "my-ft-gpt4",
            1000,
            500,
            strict=strict_from_cost_mod,
        )


def test_env_var_disables_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOVERNANCE_COST_STRICT_UNKNOWN_MODELS=false flips the default."""
    monkeypatch.setenv("GOVERNANCE_COST_STRICT_UNKNOWN_MODELS", "false")
    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
    )
    assert sdk.cost._strict_unknown_models is False


def test_env_var_sets_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """GOVERNANCE_COST_UNKNOWN_MODEL_FALLBACK_USD_PER_MILLION is parsed as float."""
    monkeypatch.setenv(
        "GOVERNANCE_COST_UNKNOWN_MODEL_FALLBACK_USD_PER_MILLION",
        "3.75",
    )
    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
    )
    assert sdk.cost._unknown_model_fallback_usd_per_million == 3.75


def test_kwarg_beats_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Precedence: explicit kwarg wins over env var."""
    monkeypatch.setenv("GOVERNANCE_COST_STRICT_UNKNOWN_MODELS", "false")
    # kwarg says True — should win.
    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
        cost_strict_unknown_models=True,
    )
    assert sdk.cost._strict_unknown_models is True


def test_invalid_env_var_value_raises_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage env-var value → ValueError at construction.

    A misconfiguration here is a silent budget-bypass risk — must be
    loud, not silently defaulted.
    """
    monkeypatch.setenv("GOVERNANCE_COST_STRICT_UNKNOWN_MODELS", "maybe")
    with pytest.raises(ValueError, match="GOVERNANCE_COST_STRICT_UNKNOWN_MODELS"):
        GovernanceSDK(
            database_url="postgresql://localhost/test",
            audit_secret=_STRONG_SECRET,
        )


def test_env_var_not_captured_at_module_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the env var after import is still honored.

    Regression lock for the module-level-env-capture anti-pattern
    (feedback_no_module_level_env_capture.md). If the SDK ever reads
    os.environ at import time, changing the env var here would have
    no effect and this test would fail.
    """
    # Ensure the env var is NOT set at import time (import already
    # happened above; this is belt-and-braces).
    monkeypatch.delenv("GOVERNANCE_COST_STRICT_UNKNOWN_MODELS", raising=False)
    monkeypatch.setenv("GOVERNANCE_COST_STRICT_UNKNOWN_MODELS", "false")

    sdk = GovernanceSDK(
        database_url="postgresql://localhost/test",
        audit_secret=_STRONG_SECRET,
    )
    assert sdk.cost._strict_unknown_models is False
