"""Tests for the OpenTelemetry audit exporter and the subscribe() hook."""
from __future__ import annotations

import secrets

import pytest

from codeatelier_governance.audit import (
    AuditEvent,
    AuditModule,
    BatchingWriter,
    InMemoryAuditStore,
)
from codeatelier_governance.audit.otel_exporter import GEN_AI_NS, OTelExporter

# These imports are deferred — the test suite still passes if opentelemetry
# is not installed. Below we mark the OTel tests as skip-if-missing.
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    _OTEL = True
except ImportError:  # pragma: no cover
    _OTEL = False


pytestmark = pytest.mark.skipif(not _OTEL, reason="opentelemetry not installed")


# ---------------------------------------------------------------------------
# Subscribe hook (no OTel dependency in this group)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_subscribe_invokes_callback_after_log() -> None:
    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()
    received = []

    async def cb(record):  # type: ignore[no-untyped-def]
        received.append(record)

    audit.subscribe(cb)
    try:
        rec = await audit.log(AuditEvent(agent_id="a", kind="k"))
    finally:
        await audit.close()
    assert len(received) == 1
    assert received[0].event_id == rec.event_id


@pytest.mark.asyncio
async def test_subscriber_failure_does_not_break_audit_log() -> None:
    """Cybersecurity invariant: a broken subscriber MUST NOT break the chain."""
    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    await audit.start()

    async def crashy(_record):  # type: ignore[no-untyped-def]
        raise RuntimeError("exporter on fire")

    audit.subscribe(crashy)
    try:
        # log() must succeed despite the subscriber raising
        rec = await audit.log(AuditEvent(agent_id="a", kind="k"))
        # Subsequent logs must still work and the chain must still link
        rec2 = await audit.log(AuditEvent(agent_id="a", session_id=rec.session_id, kind="k2"))
    finally:
        await audit.close()
    assert rec2.prev_hash == rec.hmac


# ---------------------------------------------------------------------------
# OTel exporter
# ---------------------------------------------------------------------------
def _setup_in_memory_provider():  # type: ignore[no-untyped-def]
    """Build a fresh TracerProvider with an InMemorySpanExporter."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.mark.asyncio
async def test_exporter_emits_one_span_per_audit_event() -> None:
    provider, span_exporter = _setup_in_memory_provider()
    tracer = provider.get_tracer("test")
    otel_exporter = OTelExporter(tracer=tracer)

    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    audit.subscribe(otel_exporter)
    await audit.start()

    try:
        await audit.log(AuditEvent(agent_id="a", kind="tool.call"))
        await audit.log(AuditEvent(agent_id="a", kind="tool.result"))
    finally:
        await audit.close()

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 2
    names = sorted(s.name for s in spans)
    assert names == ["audit.tool.call", "audit.tool.result"]


@pytest.mark.asyncio
async def test_exporter_attributes_match_genai_namespace() -> None:
    provider, span_exporter = _setup_in_memory_provider()
    tracer = provider.get_tracer("test")
    otel_exporter = OTelExporter(tracer=tracer)

    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    audit.subscribe(otel_exporter)
    await audit.start()

    try:
        await audit.log(
            AuditEvent(
                agent_id="billing-agent",
                kind="llm.call",
                input_hash="i" * 64,
                metadata={"model": "claude-sonnet-4-6", "tokens": 120},
            )
        )
    finally:
        await audit.close()

    span = span_exporter.get_finished_spans()[0]
    attrs = dict(span.attributes or {})
    assert attrs["gen_ai.agent.id"] == "billing-agent"
    assert attrs[f"{GEN_AI_NS}.kind"] == "llm.call"
    assert attrs[f"{GEN_AI_NS}.input_hash"] == "i" * 64
    assert attrs[f"{GEN_AI_NS}.metadata.model"] == "claude-sonnet-4-6"
    assert attrs[f"{GEN_AI_NS}.metadata.tokens"] == 120
    assert "gen_ai.audit.event_id" in attrs
    assert "gen_ai.audit.hmac" in attrs


@pytest.mark.asyncio
async def test_exporter_swallows_tracer_exceptions() -> None:
    """A misbehaving tracer must NOT break the audit chain."""

    class BrokenTracer:
        def start_span(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("tracer is wedged")

    otel_exporter = OTelExporter(tracer=BrokenTracer())  # type: ignore[arg-type]

    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    audit.subscribe(otel_exporter)
    await audit.start()
    try:
        rec1 = await audit.log(AuditEvent(agent_id="a", kind="k"))
        rec2 = await audit.log(
            AuditEvent(agent_id="a", session_id=rec1.session_id, kind="k2")
        )
    finally:
        await audit.close()
    assert rec2.prev_hash == rec1.hmac


@pytest.mark.asyncio
async def test_parent_event_id_is_emitted_when_present() -> None:
    provider, span_exporter = _setup_in_memory_provider()
    otel_exporter = OTelExporter(tracer=provider.get_tracer("test"))

    store = InMemoryAuditStore()
    writer = BatchingWriter(primary=store, batch_size=5, flush_interval_s=0.02)
    audit = AuditModule(store, secret=secrets.token_bytes(32), writer=writer)
    audit.subscribe(otel_exporter)
    await audit.start()

    try:
        with audit.session():
            root = await audit.log(AuditEvent(agent_id="a", kind="root"))
            await audit.log(
                AuditEvent(agent_id="a", kind="child", parent_event_id=root.event_id)
            )
    finally:
        await audit.close()

    spans = span_exporter.get_finished_spans()
    child_span = next(s for s in spans if s.name == "audit.child")
    attrs = dict(child_span.attributes or {})
    assert attrs[f"{GEN_AI_NS}.parent_event_id"] == str(root.event_id)
