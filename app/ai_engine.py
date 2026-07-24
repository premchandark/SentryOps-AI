"""
ai_engine.py
------------
The "brain" of SentryOps AI.

Pipeline for every /ask request:
  1. classify_intent()   -> is this an incident report, a knowledge lookup,
                            or a ticket request?
  2. retrieve_context()  -> mock RAG: pulls relevant runbook snippets from
                            a small in-memory knowledge base
  3. call_llm()          -> simulated LLM reasoning step. Wrapped with
                            tenacity retry (transient-failure resilience)
                            and a deterministic fallback responder if all
                            retries are exhausted (graceful degradation
                            instead of a 500).
  4. maybe_create_ticket() -> business action: opens a mock incident ticket
                              if the question indicates something is broken.

Every step is wrapped in its own OpenTelemetry span so a single trace_id
lets you see exactly where time was spent / where it failed.
"""

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from opentelemetry.trace import Status, StatusCode
from tenacity import (
    retry,
    stop_after_attempt,
    wait_fixed,
    retry_if_exception_type,
    RetryError,
)

from .observability import (
    tracer,
    get_logger,
    log_event,
    LLM_CALL_DURATION,
    LLM_RETRY_COUNT,
    TICKETS_CREATED,
)

logger = get_logger("sentryops.ai_engine")

# --------------------------------------------------------------------------
# Mock knowledge base (stand-in for a vector DB / RAG index)
# --------------------------------------------------------------------------
KNOWLEDGE_BASE = {
    "database": "Runbook DB-114: If DB latency spikes, check connection pool "
                "saturation first (`SHOW PROCESSLIST`), then replication lag.",
    "memory": "Runbook MEM-22: OOMKilled pods usually mean a missing resource "
              "limit or a memory leak in the last deploy. Check `kubectl top pod`.",
    "latency": "Runbook LAT-08: p99 latency regressions correlate with GC "
               "pauses or noisy-neighbor CPU throttling on shared nodes.",
    "deploy": "Runbook DEP-31: Rollback via `kubectl rollout undo` if error "
              "rate exceeds 5% within 10 minutes of a deploy.",
    "disk": "Runbook DSK-04: Disk pressure evictions - check log rotation "
            "config and orphaned container layers.",
}


class TransientLLMError(Exception):
    """Simulated transient failure from the LLM provider (timeout, 429, etc.)"""


@dataclass
class AskResult:
    answer: str
    intent: str
    matched_topics: list
    ticket: Optional[dict] = None
    llm_outcome: str = "success"  # success | retry | fallback
    retries: int = 0


def classify_intent(question: str) -> str:
    q = question.lower()
    incident_words = ["down", "outage", "failing", "error", "crash", "broken", "500", "oom"]
    if any(w in q for w in incident_words):
        return "incident"
    if "ticket" in q:
        return "ticket_request"
    return "knowledge_query"


def retrieve_context(question: str) -> list:
    q = question.lower()
    hits = [topic for topic in KNOWLEDGE_BASE if topic in q]
    return hits


# --------------------------------------------------------------------------
# Simulated LLM call. `fail_mode` lets us deterministically reproduce the
# "failing test case" for the demo instead of relying on real flaky network
# calls, while still exercising the exact same retry/fallback code path a
# real OpenAI/Anthropic timeout would hit.
# --------------------------------------------------------------------------
def _raw_llm_call(question: str, matched_topics: list, fail_mode: Optional[str]) -> str:
    if fail_mode == "timeout":
        raise TransientLLMError("Simulated upstream LLM timeout after 3s")
    if fail_mode == "rate_limit":
        raise TransientLLMError("Simulated upstream 429 rate limit")

    # tiny artificial latency so the histogram buckets look realistic
    time.sleep(random.uniform(0.05, 0.15))

    if matched_topics:
        context = " ".join(KNOWLEDGE_BASE[t] for t in matched_topics)
        return f"Based on relevant runbooks: {context}"
    return ("I don't have a specific runbook match for this. General guidance: "
            "check recent deploys, resource limits, and dependency health first.")


@retry(
    retry=retry_if_exception_type(TransientLLMError),
    stop=stop_after_attempt(3),
    wait=wait_fixed(0.2),
    reraise=True,
)
def _llm_call_with_retry(question: str, matched_topics: list, fail_mode: Optional[str]) -> str:
    try:
        return _raw_llm_call(question, matched_topics, fail_mode)
    except TransientLLMError:
        LLM_RETRY_COUNT.inc()
        log_event(logger, logging.WARNING, "llm_call_retrying",
                   question=question[:80])
        raise


def call_llm(question: str, matched_topics: list, fail_mode: Optional[str] = None):
    """Returns (answer, outcome, retry_count). Never raises - always degrades gracefully."""
    start = time.perf_counter()
    try:
        with tracer.start_as_current_span("llm_call") as span:
            span.set_attribute("llm.fail_mode", fail_mode or "none")
            answer = _llm_call_with_retry(question, matched_topics, fail_mode)
            elapsed = time.perf_counter() - start
            outcome = "success" if elapsed < 0.3 else "retry"
            LLM_CALL_DURATION.labels(outcome=outcome).observe(elapsed)
            span.set_attribute("llm.outcome", outcome)
            return answer, outcome, 0
    except (RetryError, TransientLLMError) as e:
        # NOTE: tenacity's reraise=True re-raises the ORIGINAL exception
        # (TransientLLMError) once all attempts are exhausted, not a
        # RetryError wrapper - catching only RetryError here silently let
        # the exception escape uncaught and surfaced as a 502 instead of
        # a graceful degraded response. Catching both is the fix.
        elapsed = time.perf_counter() - start
        LLM_CALL_DURATION.labels(outcome="fallback").observe(elapsed)
        original = e.last_attempt.exception() if isinstance(e, RetryError) else e
        log_event(logger, logging.ERROR, "llm_call_exhausted_retries_fallback_triggered",
                   original_error=str(original))
        fallback_answer = (
            "The reasoning engine is currently degraded (upstream LLM unavailable "
            "after 3 retries). Serving cached runbook guidance instead: "
            + (" ".join(KNOWLEDGE_BASE[t] for t in matched_topics) if matched_topics
               else "No cached match available - please escalate to on-call.")
        )
        return fallback_answer, "fallback", 3


def maybe_create_ticket(question: str, intent: str, matched_topics: list) -> Optional[dict]:
    if intent != "incident":
        return None
    severity = "high" if any(w in question.lower() for w in ["down", "outage", "crash"]) else "medium"
    ticket = {
        "ticket_id": f"INC-{uuid.uuid4().hex[:8].upper()}",
        "severity": severity,
        "summary": question[:120],
        "related_runbooks": matched_topics,
        "status": "open",
    }
    TICKETS_CREATED.labels(severity=severity).inc()
    log_event(logger, logging.INFO, "ticket_created", **ticket)
    return ticket


def process_question(question: str, fail_mode: Optional[str] = None) -> AskResult:
    with tracer.start_as_current_span("process_question") as span:
        span.set_attribute("question.length", len(question))

        with tracer.start_as_current_span("classify_intent"):
            intent = classify_intent(question)

        with tracer.start_as_current_span("retrieve_context") as rspan:
            matched_topics = retrieve_context(question)
            rspan.set_attribute("retrieval.hits", len(matched_topics))

        answer, outcome, retries = call_llm(question, matched_topics, fail_mode)

        ticket = None
        with tracer.start_as_current_span("maybe_create_ticket"):
            ticket = maybe_create_ticket(question, intent, matched_topics)

        span.set_attribute("llm.outcome", outcome)
        span.set_status(Status(StatusCode.OK))

        return AskResult(
            answer=answer,
            intent=intent,
            matched_topics=matched_topics,
            ticket=ticket,
            llm_outcome=outcome,
            retries=retries,
        )