"""Framework adapters for the Governance SDK.

Optional integrations that bridge popular LLM frameworks (LangChain, OpenAI,
Anthropic, Microsoft Agent Framework) to the governance audit, cost, and
scope enforcement modules.

Each adapter is a separate submodule with its own optional dependency:

    pip install codeatelier-governance[langchain]
    pip install codeatelier-governance[openai]
    pip install codeatelier-governance[anthropic]

The Microsoft Agent Framework (AGT) adapter has no hard dep — it duck-types
against ``ChatAgent.run`` and accepts AGT trace events as plain dicts.
"""

from codeatelier_governance.integrations.agt_wrap import (
    AGTBridge,
    AGTEventShapeError,
    wrap_agt_agent,
)

__all__ = ["AGTBridge", "AGTEventShapeError", "wrap_agt_agent"]
