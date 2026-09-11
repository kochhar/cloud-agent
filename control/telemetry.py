"""Best-effort persistence for operational telemetry."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

import config
from db import pool

logger = logging.getLogger(__name__)


def start_llm_attempt(
    *,
    session_id: str,
    call_id: str,
    attempt: int,
    provider: str,
    model: str,
    context_chars: int,
    message_count: int,
) -> str | None:
    """Insert before provider IO so a process death remains visible in-flight."""
    attempt_id = str(uuid.uuid4())
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_attempts
                    (id, session_id, call_id, attempt, provider, model,
                     context_chars, message_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    attempt_id,
                    session_id,
                    call_id,
                    attempt,
                    provider,
                    model,
                    context_chars,
                    message_count,
                ),
            )
        return attempt_id
    except Exception:
        # Observability must not turn an otherwise viable provider call into a
        # failed turn. The structured log is the fallback signal.
        logger.exception("could not start LLM telemetry row")
        return None


def finish_llm_attempt(
    attempt_id: str | None,
    *,
    outcome: str,
    is_final: bool,
    duration_ms: float,
    http_status: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tool_call_count: int | None = None,
) -> None:
    if attempt_id is None:
        return
    try:
        with pool.connection() as conn:
            conn.execute(
                """
                UPDATE llm_attempts
                   SET outcome = %s,
                       is_final = %s,
                       duration_ms = %s,
                       http_status = %s,
                       input_tokens = %s,
                       output_tokens = %s,
                       tool_call_count = %s,
                       estimated_cost_usd = %s,
                       completed_at = now()
                 WHERE id = %s AND outcome = 'in_flight'
                """,
                (
                    outcome,
                    is_final,
                    duration_ms,
                    http_status,
                    input_tokens,
                    output_tokens,
                    tool_call_count,
                    _estimated_cost(input_tokens, output_tokens),
                    attempt_id,
                ),
            )
    except Exception:
        logger.exception("could not finish LLM telemetry row")


def _estimated_cost(
    input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    if (
        input_tokens is None
        or output_tokens is None
        or config.GROK_INPUT_COST_PER_MILLION is None
        or config.GROK_OUTPUT_COST_PER_MILLION is None
    ):
        return None
    million = Decimal(1_000_000)
    return (
        Decimal(input_tokens) * Decimal(str(config.GROK_INPUT_COST_PER_MILLION))
        + Decimal(output_tokens) * Decimal(str(config.GROK_OUTPUT_COST_PER_MILLION))
    ) / million
