"""Content-free JSON logs, independent metrics/traces and versioned Decimal costs."""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Histogram

registry = CollectorRegistry()
CALLS = Counter("gateway_calls_total", "Terminal calls", ["status", "logical_model"], registry=registry)
LATENCY = Histogram("gateway_call_seconds", "Total gateway duration", registry=registry)
DEPENDENCIES = Counter(
    "gateway_dependency_failures_total", "Dependency failures", ["dependency"], registry=registry
)
tracer = trace.get_tracer("llm_gateway", "0.1.0")


class JSONFormatter(logging.Formatter):
    def format(self, record):
        fields = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "message": record.getMessage(),
            "service": "llm-gateway",
            "service_version": "0.1.0",
            "environment": os.getenv("GATEWAY_ENVIRONMENT", "development"),
            "schema_version": "1.0",
        }
        fields.update(getattr(record, "context", {}))
        # Only explicit safe context fields enter logs. Never serialize exception objects or user data.
        allowed = {
            "trace_id",
            "span_id",
            "request_id",
            "run_id",
            "step_id",
            "call_id",
            "attempt_id",
            "team_id",
            "application_id",
            "api_key_id",
            "error_code",
            "config_version",
            "attempt_no",
            "provider_id",
            "model_id",
            "delay_ms",
            "remaining_timeout_ms",
        }
        fields = {
            k: v
            for k, v in fields.items()
            if k in allowed
            or k
            in {
                "timestamp",
                "level",
                "event",
                "message",
                "service",
                "service_version",
                "environment",
                "schema_version",
            }
        }
        raw = json.dumps(fields, ensure_ascii=False, default=str)
        raw = re.sub(r"(?i)(bearer\s+|sk-[a-z_-]*)([a-z0-9._-]+)", r"\1[REDACTED]", raw)
        if len(raw.encode()) > 16384:
            fields["message"] = "Log truncated"
            fields["truncated"] = True
            raw = json.dumps({k: str(v)[:256] for k, v in fields.items()}, ensure_ascii=False)
        return raw


def setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    for name in ("llm_gateway_core", "llm_gateway_services", "llm_gateway_infrastructure", "llm_gateway_api"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # HTTP clients must not log request URLs/headers or upstream exception bodies.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def setup_tracing():
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "llm-gateway"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        return provider
    return None


class Observer:
    tracer = tracer

    def start_call(self, context):
        span = self.tracer.start_span(
            "gateway.call",
            attributes={"gateway.call_id": context.call_id, "gateway.application_id": context.application_id},
        )
        sc = span.get_span_context()
        if sc.is_valid:
            context.trace_id = f"{sc.trace_id:032x}"
        return span

    @contextmanager
    def attempt_span(self, parent, row):
        span = self.start_attempt(parent, row)
        with trace.use_span(span, end_on_exit=True):
            yield

    def start_attempt(self, parent, row):
        span = self.tracer.start_span(
            "provider.attempt",
            context=trace.set_span_in_context(parent),
            attributes={"gateway.attempt_id": row["attempt_id"], "gateway.provider_id": row["provider_id"]},
        )
        context = span.get_span_context()
        if context.is_valid:
            row["span_id"] = f"{context.span_id:016x}"
            row["parent_span_id"] = f"{parent.get_span_context().span_id:016x}"
        return span

    def record_call(self, status, logical_model, duration):
        CALLS.labels(status, logical_model).inc()
        LATENCY.observe(duration)
