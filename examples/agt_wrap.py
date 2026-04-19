"""Example: layer Code Atelier governance on top of an existing AGT agent.

Runnable against a stub ChatAgent (no `agent-framework` install required).
For a real deployment, swap :class:`_StubChatAgent` for your actual AGT
``ChatAgent`` / ``AssistantAgent`` instance — ``wrap_agt_agent`` duck-types
on ``run()`` so the call site is identical.

Run with::

    python examples/agt_wrap.py
"""
from __future__ import annotations

import asyncio
import secrets
from typing import Any

from codeatelier_governance import (
    AuditEvent,
    ScopePolicy,
    wrap_agt_agent,
)
from codeatelier_governance.audit import (
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.scope.module import ScopeModule


class _StubChatAgent:
    """A stand-in for Microsoft Agent Framework's ``ChatAgent``.

    In real code, you'd import ``ChatAgent`` from ``agent_framework`` and
    pass that instance to :func:`wrap_agt_agent`. The wrapper only relies
    on ``agent.run`` being callable.
    """

    async def run(self, tool: str, **kwargs: Any) -> str:
        return f"stub-agent-response for tool={tool}"


class _MinimalSDK:
    """Bare-bones SDK stand-in. In production use ``GovernanceSDK(...)``."""

    def __init__(self, audit: AuditModule, scope: ScopeModule) -> None:
        self.audit = audit
        self.scope = scope


async def main() -> None:
    # 1. Wire a minimal in-memory audit chain (use GovernanceSDK(database_url=...)
    #    in production).
    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=1, flush_interval_s=0.01)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()

    # 2. Register a scope policy — the AGT agent may ONLY use read_ticket.
    scope = ScopeModule(audit)
    scope.register(
        ScopePolicy(
            agent_id="support-v1",
            allowed_tools=frozenset({"read_ticket"}),
        )
    )
    sdk = _MinimalSDK(audit=audit, scope=scope)

    # 3. Wrap the AGT agent. ONE LINE. Existing call sites unchanged.
    agent = _StubChatAgent()
    wrapped = wrap_agt_agent(agent, sdk, agent_id="support-v1")  # type: ignore[arg-type]

    # 4. Allowed tool call — flows through governance gates, then AGT runs.
    result = await wrapped.run(tool="read_ticket")
    print(f"[allowed]  AGT response: {result}")

    # 5. Disallowed tool call — fails CLOSED before AGT hits the network.
    #    A ``scope.violation`` audit event lands in the HMAC-chained log.
    try:
        await wrapped.run(tool="delete_ticket")
    except Exception as exc:
        print(f"[blocked]  governance gate denied: {type(exc).__name__}: {exc}")

    # 6. Emit a custom Article 12 evidence event alongside AGT telemetry.
    await audit.log(
        AuditEvent(
            agent_id="support-v1",
            kind="custom",
            metadata={"note": "business-level evidence row for compliance"},
        )
    )

    await audit._writer.flush()
    print(f"[audit]    {len(store._events)} events in the HMAC-chained log")

    await audit.close()


if __name__ == "__main__":
    asyncio.run(main())
