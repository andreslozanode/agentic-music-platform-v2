"""Prometheus metrics and OpenTelemetry tracing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

REQUESTS = Counter("agent_requests_total", "Governed agent requests", ["status"])
GUARDRAIL_EVENTS = Counter("agent_guardrail_events_total", "Guardrail decisions", ["rule"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls", ["tool", "outcome"])
LLM_TOKENS = Counter("agent_llm_tokens_total", "LLM tokens consumed", ["provider"])
LATENCY = Histogram(
    "agent_request_seconds",
    "End-to-end agent latency",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40),
)
CYCLES = Counter("agent_autonomous_cycles_total", "Autonomous cycles", ["outcome"])
CYCLE_SECONDS = Histogram("agent_autonomous_cycle_seconds", "Autonomous cycle duration")
DQ_FAILURES = Counter("agent_data_quality_failures_total", "Critical DQ failures", ["table"])
FAIRNESS_FLAGS = Counter("agent_responsible_ai_flags_total", "Responsible AI flags", ["flag"])

tracer = trace.get_tracer("autonomous_agent")


def configure_tracing(service_name: str, otlp_endpoint: str | None) -> None:
    """Install an OTLP exporter when an endpoint is configured (optional ``otlp`` extra)."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:  # pragma: no cover - requires collector
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)


@contextmanager
def span(name: str, **attributes: str | int | float | bool) -> Iterator[None]:
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield
