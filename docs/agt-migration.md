# Migrate from Microsoft Agent Framework to Code Atelier in 10 minutes

If you already ship on Microsoft Agent Framework (AGT) and your
compliance team is asking about EU AI Act Article 12 evidence, this
guide adds an HMAC-chained audit path to your existing deployment. No
rip-and-replace. No new infrastructure. One line at the agent call site.

## Why add Code Atelier on top of AGT

AGT gives you a runtime, DevUI, Aspire integration, and first-class
human-in-the-loop gates. What it does not give you today is a
tamper-evident audit chain, signed single-use approval tokens, or a
one-command Article 12 evidence bundle. Code Atelier fills that layer.

We exceed Article 12, we do not conflate with it: the law requires
automatic event logging with six-month retention; our HMAC chain adds
tamper-evidence on top. Your AGT deployment keeps working; Code
Atelier gates run **before** each call.

## Install

`pip install codeatelier-governance`. No `[agt]` extra. The adapter
duck-types against `ChatAgent.run` and accepts AGT telemetry as plain
dictionaries — AGT stays a peer dependency of your application.

## Step 1: wrap your existing AGT agent

Your AGT call site stays identical. Add one wrap line and a scope
policy declaring what the agent may do. A disallowed tool raises
`ScopeViolation` **before** AGT touches the network, and a
`scope.violation` event lands in your HMAC chain with agent id,
session id, tool name, prev-hash, and a rotating HMAC.

```python
from agent_framework import ChatAgent
from codeatelier_governance import GovernanceSDK, ScopePolicy, wrap_agt_agent

sdk = GovernanceSDK(database_url="postgresql://...")
sdk.scope.register(ScopePolicy(
    agent_id="support-v1",
    allowed_tools=frozenset({"read_ticket"}),  # refund_order NOT included
))

agent = ChatAgent(name="support-v1", tools=[read_ticket, refund_order])
agent = wrap_agt_agent(agent, sdk, agent_id="support-v1")

result = await agent.run(user_query)  # scope + budget + halt gates run first
```

The wrap patches `agent.run` in place. Existing `isinstance(agent,
ChatAgent)` checks downstream keep working.

## Step 2: ingest AGT trace events into the audit chain

If your AGT deployment already ships OTel-style traces to Aspire,
Azure Monitor, or a local OpenTelemetry collector, you can mirror
those spans into the audit chain without touching your call sites.
Subscribe to your existing exporter and hand each event to the
bridge. Three AGT span kinds are recognised: `tool_call`, `llm_call`,
`hitl_approval`. The HITL shape auto-maps to `approval.granted` /
`approval.denied` / `approval.requested` from the span's `outcome`
field. Unknown kinds are logged and skipped — upstream AGT additions
never break your pipeline.

```python
from codeatelier_governance.integrations.agt_wrap import AGTBridge

bridge = AGTBridge(sdk=sdk, agent_id="support-v1")

async def on_agt_span(span_dict: dict) -> None:
    await bridge.consume(span_dict)
```

## Step 3: export Article 12 evidence for a session

The chain is now populated automatically on every wrapped call. To
produce an Article 12 evidence bundle for a single session — the
deliverable your general counsel asks for when a regulator calls —
point the CLI at the session id. You get a signed ZIP: HMAC-verifiable
manifest, full event sequence, any halt rows, a chain-contiguity
proof, and a cover page with the verifier command a regulator can
re-run independently.

```bash
governance report --session-id <uuid> --format article12
```

The CLI works against sessions your AGT deployment produces because
they land in the same Postgres the SDK writes to.

## What you keep; what you get

You keep: AGT runtime, DevUI, Aspire dashboards, HITL gates, schedulers,
and every tool registration already in your code base.

You get: HMAC-chained append-only audit, default-deny scope enforcement
per agent, per-session and per-agent/day USD caps, signed single-use
approval tokens, a tamper-evident verify command, and a one-command
Article 12 evidence bundle.

The runnable reference lives at `examples/agt_wrap.py`. Email
`hello@codeatelier.tech` or visit `codeatelier.tech/governance` to book
a 30-minute live tamper-detection demo against your own Postgres.
