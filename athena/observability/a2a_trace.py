"""OpenTelemetry spans for the ADK-to-CrewAI takeover."""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry.sdk.trace import Span, TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id, set_span_in_context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# SQLite events carry the span linkage even in a deployment without an exporter.
_TRACER = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False).get_tracer(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()
_ACTIVE: ContextVar[Span | None] = ContextVar("athena_a2a_span", default=None)


@contextmanager
def a2a_span(kind: SpanKind, headers: Mapping[str, str] | None = None):
    """Keep the boundary span in scope for both OpenTelemetry and the event log."""
    parent = _PROPAGATOR.extract(headers or {}) if kind == SpanKind.SERVER else None
    with _TRACER.start_as_current_span(
        "athena.a2a.send" if kind == SpanKind.CLIENT else "athena.a2a.receive",
        kind=kind,
        context=parent,
    ) as span:
        token = _ACTIVE.set(span if isinstance(span, Span) else None)
        try:
            yield
        finally:
            _ACTIVE.reset(token)


def outgoing_trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    span = _ACTIVE.get()
    if span is not None and span.kind == SpanKind.CLIENT:
        _PROPAGATOR.inject(headers, context=set_span_in_context(span))
    return headers


def trace_fields() -> dict[str, str]:
    span = _ACTIVE.get()
    if span is None:
        return {}
    context = span.get_span_context()
    fields = {
        "trace_id": format_trace_id(context.trace_id),
        "span_id": format_span_id(context.span_id),
        "span_kind": span.kind.name.lower(),
    }
    if span.parent is not None:
        fields["parent_span_id"] = format_span_id(span.parent.span_id)
    return fields
