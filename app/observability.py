"""
observability.py
-----------------
Central instrumentation module for SentryOps AI.

Provides:
  - contextvars-based request_id / trace_id propagation (works correctly
    under async concurrency, unlike thread-locals)
  - a JSON structured logging formatter (log-shipping friendly: Loki /
    CloudWatch / Datadog can all parse this without a custom pipeline)
  - an OpenTelemetry TracerProvider wired to a console exporter for the
    demo (swap for OTLPSpanExporter -> Jaeger/Tempo in real deployment,
    see comment below)
  - Prometheus metric objects shared across the app
"""

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)
from prometheus_client import Counter, Histogram, Gauge

# --------------------------------------------------------------------------
# Request-scoped context (propagates across async calls within one request)
# --------------------------------------------------------------------------
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------
# Structured JSON logging
# --------------------------------------------------------------------------
class JSONLogFormatter(logging.Formatter):
    """Emits one JSON object per line -> trivially parsed by Loki/ELK/CloudWatch."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
            "trace_id": trace_id_ctx.get(),
        }
        # allow callers to attach arbitrary structured fields via `extra={"extra_fields": {...}}`
        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONLogFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields):
    """Helper so call sites read cleanly: log_event(logger, logging.INFO, 'ticket_created', ticket_id=x)"""
    logger.log(level, message, extra={"extra_fields": fields})


# --------------------------------------------------------------------------
# OpenTelemetry tracing setup
# --------------------------------------------------------------------------
# In production this ConsoleSpanExporter would be swapped for:
#   from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
#   BatchSpanProcessor(OTLPSpanExporter(endpoint="http://jaeger-collector:4317"))
# so spans ship to Jaeger/Tempo/Grafana instead of stdout. Kept as console
# exporter here so the demo is fully self-contained with no extra infra.
resource = Resource.create({"service.name": "sentryops-ai"})
provider = TracerProvider(resource=resource)

# IMPORTANT: spans are written to their own file, not stdout, and NOT
# interleaved with the JSON application logs. Mixing raw span dumps into
# the same stream as structured logs breaks every log-shipping parser
# (Loki/ELK/CloudWatch expect one-JSON-object-per-line) - this is exactly
# the kind of subtle observability-pipeline bug this project is meant to
# surface (see the "debugging insight" section of the README).
import os
import tempfile

# Cross‑platform safe log path
tmp_dir = tempfile.gettempdir()   # Windows -> C:\Users\<user>\AppData\Local\Temp, Linux -> /tmp
log_path = os.path.join(tmp_dir, "sentryops_traces.log")

_trace_log_file = open(log_path, "a", buffering=1)
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter(out=_trace_log_file)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sentryops.tracer")


# --------------------------------------------------------------------------
# Prometheus metrics (shared singletons, imported by main.py and services)
# --------------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "sentryops_requests_total",
    "Total HTTP requests",
    ["endpoint", "method", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "sentryops_request_latency_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

ERROR_COUNT = Counter(
    "sentryops_errors_total",
    "Total errors by type",
    ["endpoint", "error_type"],
)

IN_PROGRESS = Gauge(
    "sentryops_requests_in_progress",
    "Requests currently being processed",
    ["endpoint"],
)

LLM_CALL_DURATION = Histogram(
    "sentryops_llm_call_duration_seconds",
    "Time spent in the LLM/reasoning layer",
    ["outcome"],  # success | retry | fallback
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

LLM_RETRY_COUNT = Counter(
    "sentryops_llm_retries_total",
    "Number of LLM call retries triggered",
)

TICKETS_CREATED = Counter(
    "sentryops_tickets_created_total",
    "Business action: incident tickets created",
    ["severity"],
)


class Timer:
    """Small context manager to measure elapsed wall-clock time in seconds."""

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.perf_counter() - self._start