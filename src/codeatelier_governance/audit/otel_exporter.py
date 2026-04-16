"""OpenTelemetry exporter for audit events.

Translates each :class:`AuditEventRecord` into an OpenTelemetry span using
GenAI semantic conventions, allowing the audit substrate to feed into any
existing OTel-compatible observability stack (Datadog, Honeycomb, Grafana
Tempo, Arize Phoenix, ...).

Usage:
    from codeatelier_governance.audit.otel_exporter import OTelExporter
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())  # configure exporters here
    exporter = OTelExporter()
    sdk.audit.subscribe(exporter)

This module requires the optional ``[otel]`` install:

    pip install codeatelier-governance[otel]

Design notes:
    * Audit events are point-in-time records, not durations. Each event is
      emitted as a zero-duration span. v0.2 may correlate ``*.start`` and
      ``*.end`` events into a single duration span.
    * The exporter does NOT configure any tracer provider on its own — it
      uses whatever the host application has already set up. If the host
      has not configured a tracer provider, OTel's no-op fallback applies
      and spans are silently dropped (correct OTel behavior).
    * Network egress is the host application's responsibility (via the
      provider's span processors). The exporter never opens sockets.
    * Cybersecurity invariant: a failing OTel call MUST NOT break the
      audit log. AuditModule.subscribe() catches exceptions from this
      callback. We additionally catch within the callback itself for
      defensive depth.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..security.redaction import redact_secrets
from .models import AuditEventRecord

try:
    from opentelemetry import trace as _otel_trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the import-error test
    _OTEL_AVAILABLE = False

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer


GEN_AI_NS = "gen_ai.audit"


class OTelExporter:
    """Audit event → OpenTelemetry span exporter.

    Pass an instance to :meth:`AuditModule.subscribe` and it will receive
    every successfully logged audit event.
    """

    def __init__(self, tracer: "Tracer | None" = None) -> None:
        if not _OTEL_AVAILABLE:
            raise ImportError(
                "OTelExporter requires opentelemetry-api and opentelemetry-sdk. "
                "Install with: pip install codeatelier-governance[otel]"
            )
        self._tracer = tracer or _otel_trace.get_tracer(
            "codeatelier_governance.audit"
        )

    async def __call__(self, record: AuditEventRecord) -> None:
        """Emit one audit record as an OTel span.

        Defensive: any OTel-side exception is swallowed so a misbehaving
        exporter cannot break the audit chain.
        """
        try:
            attrs = self._build_attributes(record)
            span = self._tracer.start_span(
                name=f"audit.{record.kind}",
                attributes=attrs,
            )
            span.end()
        except Exception:  # noqa: BLE001 - audit chain integrity > exporter errors
            # AuditModule.subscribe() also catches; this is defense in depth.
            return

    @staticmethod
    def _build_attributes(record: AuditEventRecord) -> dict[str, Any]:
        """Map an audit record to OTel attribute keys.

        Uses the ``gen_ai.audit.*`` namespace to coexist cleanly with the
        official OTel GenAI semantic conventions (which use ``gen_ai.*``
        for instrumentation by frameworks). Our governance metadata is
        scoped under ``gen_ai.audit.*`` so it is identifiable as audit
        substrate output, not framework instrumentation.
        """
        attrs: dict[str, Any] = {
            f"{GEN_AI_NS}.event_id": str(record.event_id),
            f"{GEN_AI_NS}.session_id": str(record.session_id),
            f"{GEN_AI_NS}.kind": record.kind,
            f"{GEN_AI_NS}.hmac": record.hmac,
            "gen_ai.agent.id": record.agent_id,
        }
        if record.parent_event_id is not None:
            attrs[f"{GEN_AI_NS}.parent_event_id"] = str(record.parent_event_id)
        if record.input_hash is not None:
            attrs[f"{GEN_AI_NS}.input_hash"] = record.input_hash
        if record.output_hash is not None:
            attrs[f"{GEN_AI_NS}.output_hash"] = record.output_hash
        if record.prev_hash is not None:
            attrs[f"{GEN_AI_NS}.prev_hash"] = record.prev_hash
        # Redact secret-shaped substrings BEFORE flattening. ``sanitize_metadata``
        # already ran upstream (NFC/ANSI-strip/size caps) but does NOT scrub
        # ``sk-ant-...`` / ``AKIA...`` / DSN-embedded credentials. Without this
        # step any raw secret that accidentally landed in a stack trace or
        # error message in ``metadata`` would leak out of the audit substrate
        # over the host's OTel pipe (Datadog, Honeycomb, etc.).
        # ``redact_secrets`` is recursive — it walks nested dicts/lists — so
        # we call it once on the top-level ``metadata`` dict and then flatten.
        redacted_metadata: dict[str, Any] = redact_secrets(record.metadata)
        # Stringify metadata. OTel attribute values must be primitives;
        # nested dicts and lists are flattened with str() to keep the
        # implementation tiny and predictable.
        for key, value in redacted_metadata.items():
            if value is None:
                continue
            attrs[f"{GEN_AI_NS}.metadata.{key}"] = (
                value if isinstance(value, (str, int, float, bool)) else str(value)
            )
        return attrs
