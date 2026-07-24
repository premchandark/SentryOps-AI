"""
main.py
-------
SentryOps AI - an AI-powered SRE incident copilot.

POST /ask       -> ask a question; may trigger the business action of
                    opening an incident ticket if the question describes
                    an outage/failure.
GET  /health    -> liveness/readiness probe (checks dependency + uptime)
GET  /metrics   -> Prometheus exposition format
GET  /tickets   -> list mock tickets created so far (lets you show the
                    "business action" side-effect in the demo)

Run:
    uvicorn app.main:app --reload --port 8000
"""

import logging
import time
import uuid
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .observability import (
    request_id_ctx,
    trace_id_ctx,
    new_request_id,
    get_logger,
    log_event,
    tracer,
    REQUEST_COUNT,
    REQUEST_LATENCY,
    ERROR_COUNT,
    IN_PROGRESS,
)
from .ai_engine import process_question

logger = get_logger("sentryops.main")

app = FastAPI(title="SentryOps AI", version="1.0.0")

START_TIME = time.time()
TICKET_STORE = []  # in-memory mock "ticketing system"


# --------------------------------------------------------------------------
# Middleware: assigns a request_id + trace_id to EVERY request (even ones
# that fail validation before reaching the route), times it, logs a
# structured access-log line, and records Prometheus metrics. This is what
# lets you correlate a log line, a metric spike, and a trace for the same
# request during debugging.
# --------------------------------------------------------------------------
@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    incoming_id = request.headers.get("x-request-id")
    req_id = incoming_id or new_request_id()
    request_id_ctx.set(req_id)

    endpoint = request.url.path
    IN_PROGRESS.labels(endpoint=endpoint).inc()
    start = time.perf_counter()

    with tracer.start_as_current_span(f"{request.method} {endpoint}") as span:
        span.set_attribute("http.method", request.method)
        span.set_attribute("http.route", endpoint)
        span.set_attribute("request_id", req_id)
        trace_id_ctx.set(format(span.get_span_context().trace_id, "032x"))

        log_event(logger, logging.INFO, "request_started",
                   method=request.method, path=endpoint)

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            status_code = 500
            ERROR_COUNT.labels(endpoint=endpoint, error_type=type(exc).__name__).inc()
            log_event(logger, logging.ERROR, "unhandled_exception",
                       error=str(exc), error_type=type(exc).__name__)
            span.record_exception(exc)
            elapsed = time.perf_counter() - start
            REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
            REQUEST_COUNT.labels(endpoint=endpoint, method=request.method,
                                  status_code=status_code).inc()
            IN_PROGRESS.labels(endpoint=endpoint).dec()
            return JSONResponse(
                status_code=500,
                content={"error": "internal_server_error", "request_id": req_id},
                headers={"x-request-id": req_id},
            )

        elapsed = time.perf_counter() - start
        REQUEST_LATENCY.labels(endpoint=endpoint).observe(elapsed)
        REQUEST_COUNT.labels(endpoint=endpoint, method=request.method,
                              status_code=status_code).inc()
        if status_code >= 400:
            ERROR_COUNT.labels(endpoint=endpoint, error_type=f"http_{status_code}").inc()

        IN_PROGRESS.labels(endpoint=endpoint).dec()
        response.headers["x-request-id"] = req_id
        log_event(logger, logging.INFO, "request_completed",
                   status_code=status_code, latency_ms=round(elapsed * 1000, 2))
        return response


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    # optional demo hook to deterministically reproduce LLM failure modes
    # for the "failing test case" without needing a real flaky dependency
    simulate_failure: Optional[str] = Field(
        default=None, description="one of: timeout, rate_limit"
    )

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank/whitespace-only")
        return v


class AskResponse(BaseModel):
    request_id: str
    intent: str
    answer: str
    matched_runbooks: list
    llm_outcome: str
    retries: int
    ticket: Optional[dict] = None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest):
    req_id = request_id_ctx.get()
    with tracer.start_as_current_span("ask_handler"):
        try:
            result = process_question(payload.question, fail_mode=payload.simulate_failure)
        except Exception as exc:
            ERROR_COUNT.labels(endpoint="/ask", error_type=type(exc).__name__).inc()
            log_event(logger, logging.ERROR, "ask_processing_failed", error=str(exc))
            raise HTTPException(status_code=502, detail="AI processing pipeline failed") from exc

        if result.ticket:
            TICKET_STORE.append(result.ticket)

        return AskResponse(
            request_id=req_id,
            intent=result.intent,
            answer=result.answer,
            matched_runbooks=result.matched_topics,
            llm_outcome=result.llm_outcome,
            retries=result.retries,
            ticket=result.ticket,
        )


@app.get("/health")
async def health():
    uptime = round(time.time() - START_TIME, 2)
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "tickets_in_store": len(TICKET_STORE),
    }


@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/tickets")
async def list_tickets():
    return {"count": len(TICKET_STORE), "tickets": TICKET_STORE}