"""Built-in model pricing table for automatic cost estimation.

Prices are in USD per 1M tokens.

**Unknown-model policy (v0.6.2 hardening):** By default ``estimate_cost``
raises :class:`UnknownModelError` rather than silently returning 0.0.
Silent-zero was a budget-bypass vector — a fine-tuned or custom model
name (``my-ft-gpt4``) would never trip any USD cap. Callers that want
the old "log-and-ignore" behavior must pass ``strict=False``, which
emits a structlog warning and optionally uses a configured fallback
per-token rate.

Prefix matching: if the exact model string is not found, we try matching
by prefix (longest match wins). This handles versioned model names like
``gpt-4o-2024-05-13`` matching the ``gpt-4o`` entry, and dated Anthropic
variants like ``claude-opus-4-7-20260415`` matching ``claude-opus-4-7``.
"""
from __future__ import annotations

import structlog

from .errors import UnknownModelError

logger = structlog.get_logger(__name__)

MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    "o1": {"input": 15.00, "output": 60.00},
    "o1-mini": {"input": 3.00, "output": 12.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # Anthropic
    # NOTE (v0.6.2): claude-opus-4-7 pricing held at parity with Opus 4.6
    # ($15/1M input, $75/1M output) per Anthropic's public pricing page as
    # of 2026-04-17. If Anthropic revises this after GA, update here.
    # Source: https://www.anthropic.com/pricing#api (checked 2026-04-17)
    "claude-opus-4-7": {"input": 15.00, "output": 75.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "claude-3.5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3.5-haiku": {"input": 0.80, "output": 4.00},
    # Google
    "gemini-1.5-pro": {"input": 3.50, "output": 10.50},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    # Meta (via API providers)
    "llama-3.1-405b": {"input": 3.00, "output": 3.00},
    "llama-3.1-70b": {"input": 0.80, "output": 0.80},
    "llama-3.1-8b": {"input": 0.10, "output": 0.10},
    # Mistral
    "mistral-large": {"input": 2.00, "output": 6.00},
    "mistral-medium": {"input": 2.70, "output": 8.10},
    "mistral-small": {"input": 0.20, "output": 0.60},
}

# Pre-sorted by longest prefix first for matching
_SORTED_PREFIXES: list[str] = sorted(MODEL_PRICING.keys(), key=len, reverse=True)


def _resolve_pricing(model: str) -> dict[str, float] | None:
    """Look up pricing for ``model``, falling back to the longest prefix match."""
    pricing = MODEL_PRICING.get(model)
    if pricing is not None:
        return pricing
    for prefix in _SORTED_PREFIXES:
        if model.startswith(prefix):
            return MODEL_PRICING[prefix]
    return None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    strict: bool = True,
    fallback_usd_per_million: float | None = None,
) -> float:
    """Estimate USD cost from model name + token counts.

    Uses prefix matching so that versioned model names (e.g.
    ``gpt-4o-2024-05-13`` or ``claude-opus-4-7-20260415``) match the base
    entry (longest prefix wins).

    Unknown-model policy:
        * ``strict=True`` (default): raise :class:`UnknownModelError`.
          Silent-zero costing on unknown models is a budget-bypass vector —
          a fine-tuned name ``my-ft-gpt4`` would never trip any USD cap.
        * ``strict=False``: emit a structlog warning
          ``cost.unknown_model`` and apply ``fallback_usd_per_million``
          (defaults to 0.0 if not provided — this is a foot-gun, acknowledge
          it) uniformly to both input and output tokens.

    Args:
        model: Model identifier (exact or prefix match).
        input_tokens: Prompt token count.
        output_tokens: Completion token count.
        strict: Whether to raise on unknown models. Default True.
        fallback_usd_per_million: Per-1M rate to apply when ``strict=False``
            and the model is unknown. Used for BOTH input and output.
            If None, falls back to 0.0 (explicit foot-gun; warning still fires).

    Raises:
        UnknownModelError: if ``strict=True`` and ``model`` is not in the
            table (and no prefix matches).
    """
    pricing = _resolve_pricing(model)
    if pricing is None:
        if strict:
            raise UnknownModelError(
                f"No pricing entry for model {model!r}. "
                "Silent-zero costing would bypass USD budget caps. "
                "Fix: add the model to MODEL_PRICING, or pass "
                "strict=False with fallback_usd_per_million=N to opt into "
                "lax mode (NOT recommended for production).",
                recovery_hint=(
                    "Add pricing for this model, or call with "
                    "strict=False and a non-zero fallback_usd_per_million."
                ),
            )
        rate = fallback_usd_per_million if fallback_usd_per_million is not None else 0.0
        logger.warning(
            "cost.unknown_model",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            fallback_usd_per_million=rate,
            detail=(
                "Unknown model priced at fallback rate. "
                "This is a foot-gun — an attacker naming a model the SDK "
                "does not know can bypass USD caps if the fallback is 0. "
                "Add the model to MODEL_PRICING for accurate accounting."
            ),
        )
        return (input_tokens + output_tokens) * rate / 1_000_000
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000
