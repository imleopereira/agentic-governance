"""Built-in model pricing table for automatic cost estimation.

Prices are in USD per 1M tokens. When a model is not found in the table,
``estimate_cost`` returns 0.0 rather than raising — unknown models are
silently zero-costed so the host application continues unblocked.

Prefix matching: if the exact model string is not found, we try matching
by prefix (longest match wins). This handles versioned model names like
``gpt-4o-2024-05-13`` matching the ``gpt-4o`` entry.
"""
from __future__ import annotations

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


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost from model name + token counts.

    Returns 0.0 for unknown models. Uses prefix matching so that
    versioned model names (e.g. ``gpt-4o-2024-05-13``) match the
    base entry.
    """
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        for prefix in _SORTED_PREFIXES:
            if model.startswith(prefix):
                pricing = MODEL_PRICING[prefix]
                break
    if pricing is None:
        return 0.0
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000
