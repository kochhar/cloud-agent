"""Fixed, bounded aggregation queries for the operations dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import config


def _number(value: Any) -> float | int | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return round(value.total_seconds(), 3)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row_numbers(row: dict) -> dict:
    return {key: _number(value) for key, value in row.items()}


def collect_overview(conn, hours: int) -> dict:
    """Read one dashboard snapshot from Postgres.

    Every query is fixed and window-bounded. Callers validate `hours`; there
    is intentionally no endpoint that accepts SQL, table names, or filters.
    """
    activity = conn.execute(
        """
        WITH bounds AS (
            SELECT date_trunc('hour', now()) -
                       (%s * interval '1 hour') AS first_hour
        ),
        hours AS (
            SELECT generate_series(first_hour, date_trunc('hour', now()),
                                   interval '1 hour') AS hour
              FROM bounds
        ),
        starts AS (
            SELECT date_trunc('hour', created_at) AS hour, count(*) AS count
              FROM sessions, bounds
             WHERE created_at >= first_hour
             GROUP BY 1
        ),
        outcomes AS (
            SELECT date_trunc('hour', created_at) AS hour,
                   count(*) FILTER (WHERE payload ->> 'status' = 'idle') AS completed,
                   count(*) FILTER (WHERE payload ->> 'status' = 'failed') AS failed
              FROM events, bounds
             WHERE type = 'status'
               AND payload ->> 'status' IN ('idle', 'failed')
               AND created_at >= first_hour
             GROUP BY 1
        )
        SELECT h.hour,
               coalesce(s.count, 0) AS sessions_started,
               coalesce(o.completed, 0) AS turns_completed,
               coalesce(o.failed, 0) AS turns_failed
          FROM hours h
          LEFT JOIN starts s USING (hour)
          LEFT JOIN outcomes o USING (hour)
         ORDER BY h.hour
        """,
        (hours - 1,),
    ).fetchall()
    activity = [_row_numbers(row) for row in activity]
    completed = sum(row["turns_completed"] for row in activity)
    failed = sum(row["turns_failed"] for row in activity)
    outcomes = completed + failed

    llm = conn.execute(
        """
        SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY duration_ms)
                   FILTER (WHERE outcome = 'success') AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
                   FILTER (WHERE outcome = 'success') AS p95_ms,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms)
                   FILTER (WHERE outcome = 'success') AS p99_ms,
               count(*) FILTER (WHERE outcome <> 'in_flight')
                   AS completed_attempts,
               count(*) FILTER (WHERE outcome NOT IN ('in_flight', 'success'))
                   AS failed_attempts,
               count(*) FILTER (WHERE is_final) AS final_calls,
               count(*) FILTER (WHERE is_final AND outcome <> 'success')
                   AS failed_final_calls,
               count(*) FILTER (WHERE outcome = 'success') AS successful_attempts,
               count(estimated_cost_usd) FILTER (WHERE outcome = 'success')
                   AS priced_attempts,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               sum(estimated_cost_usd) AS estimated_cost_usd,
               count(*) FILTER (WHERE outcome = 'in_flight') AS in_flight,
               count(*) FILTER (
                   WHERE outcome = 'in_flight'
                     AND started_at < now() - interval '10 minutes'
               ) AS stale_in_flight
          FROM llm_attempts
         WHERE started_at >= now() - (%s * interval '1 hour')
        """,
        (hours,),
    ).fetchone()
    llm = _row_numbers(llm)
    llm["attempt_error_rate"] = (
        llm["failed_attempts"] / llm["completed_attempts"]
        if llm["completed_attempts"]
        else None
    )
    llm["final_call_error_rate"] = (
        llm["failed_final_calls"] / llm["final_calls"]
        if llm["final_calls"]
        else None
    )
    llm_errors = conn.execute(
        """
        SELECT outcome, count(*) AS count
          FROM llm_attempts
         WHERE started_at >= now() - (%s * interval '1 hour')
           AND outcome NOT IN ('in_flight', 'success')
         GROUP BY outcome
         ORDER BY count DESC, outcome
        """,
        (hours,),
    ).fetchall()
    llm["errors"] = [_row_numbers(row) for row in llm_errors]

    priced = conn.execute(
        """
        SELECT coalesce(sum(
                   (coalesce(input_tokens, 0) * %s
                    + coalesce(output_tokens, 0) * %s)
                   * CASE WHEN input_tokens >= %s THEN %s ELSE 1 END
                   / 1000000.0
               ), 0) AS estimated_cost_usd,
               count(*) FILTER (
                   WHERE outcome = 'success'
                     AND input_tokens IS NOT NULL
                     AND output_tokens IS NOT NULL
               ) AS priced_attempts
          FROM llm_attempts
         WHERE started_at >= now() - (%s * interval '1 hour')
           AND outcome = 'success'
        """,
        (
            config.INPUT_COST_PER_MILLION,
            config.OUTPUT_COST_PER_MILLION,
            config.LONG_CONTEXT_TOKENS,
            config.LONG_CONTEXT_MULTIPLIER,
            hours,
        ),
    ).fetchone()
    priced = _row_numbers(priced)
    llm["estimated_cost_usd"] = priced["estimated_cost_usd"]
    llm["priced_attempts"] = priced["priced_attempts"]
    cost_available = priced["priced_attempts"] > 0 and completed > 0

    active_states = conn.execute(
        """
        SELECT status, count(*) AS count
          FROM sessions
         WHERE status IN ('idle', 'thinking', 'executing')
         GROUP BY status
         ORDER BY status
        """
    ).fetchall()

    queue = conn.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'pending') AS pending_count,
               count(*) FILTER (
                   WHERE status = 'pending' AND attempts = 0
               ) AS never_dispatched_count,
               extract(epoch FROM (
                   now() - min(created_at) FILTER (
                       WHERE status = 'pending' AND attempts = 0
                   )
               )) AS oldest_never_dispatched_seconds
          FROM tool_calls
        """
    ).fetchone()

    recovery = conn.execute(
        """
        WITH deaths AS (
            SELECT session_id, min(created_at) AS first_death_at, count(*) AS deaths
              FROM events
             WHERE type = 'sandbox_died'
               AND created_at >= now() - (%s * interval '1 hour')
             GROUP BY session_id
        ),
        classified AS (
            SELECT d.*,
                   EXISTS (
                       SELECT 1 FROM events e
                        WHERE e.session_id = d.session_id
                          AND e.type = 'status'
                          AND e.payload ->> 'status' = 'idle'
                          AND e.created_at > d.first_death_at
                   ) AS recovered,
                   EXISTS (
                       SELECT 1 FROM events e
                        WHERE e.session_id = d.session_id
                          AND e.type = 'status'
                          AND e.payload ->> 'status' = 'failed'
                          AND e.created_at > d.first_death_at
                   ) AS failed
              FROM deaths d
        )
        SELECT coalesce(sum(deaths), 0) AS deaths,
               count(*) AS affected_sessions,
               count(*) FILTER (WHERE recovered) AS recovered_sessions,
               count(*) FILTER (WHERE failed AND NOT recovered) AS failed_sessions,
               count(*) FILTER (WHERE NOT recovered AND NOT failed) AS unresolved_sessions
          FROM classified
        """,
        (hours,),
    ).fetchone()
    recovery = _row_numbers(recovery)
    resolved = recovery["recovered_sessions"] + recovery["failed_sessions"]
    recovery["success_rate"] = (
        recovery["recovered_sessions"] / resolved if resolved else None
    )

    reexecution = conn.execute(
        """
        SELECT count(*) FILTER (WHERE attempts > 0) AS dispatched_calls,
               count(*) FILTER (WHERE attempts > 1) AS reexecuted_calls
          FROM tool_calls
         WHERE coalesce(dispatched_at, created_at) >=
               now() - (%s * interval '1 hour')
        """,
        (hours,),
    ).fetchone()
    reexecution = _row_numbers(reexecution)
    reexecution["rate"] = (
        reexecution["reexecuted_calls"] / reexecution["dispatched_calls"]
        if reexecution["dispatched_calls"]
        else None
    )

    latency = conn.execute(
        """
        SELECT
          percentile_cont(0.5) WITHIN GROUP (
              ORDER BY extract(epoch FROM (dispatched_at - created_at))
          ) FILTER (WHERE attempts = 1) AS dispatch_p50_seconds,
          percentile_cont(0.95) WITHIN GROUP (
              ORDER BY extract(epoch FROM (dispatched_at - created_at))
          ) FILTER (WHERE attempts = 1) AS dispatch_p95_seconds,
          percentile_cont(0.5) WITHIN GROUP (
              ORDER BY extract(epoch FROM (completed_at - dispatched_at))
          ) FILTER (WHERE completed_at IS NOT NULL) AS observed_execution_p50_seconds,
          percentile_cont(0.95) WITHIN GROUP (
              ORDER BY extract(epoch FROM (completed_at - dispatched_at))
          ) FILTER (WHERE completed_at IS NOT NULL) AS observed_execution_p95_seconds,
          count(*) FILTER (WHERE attempts = 1) AS dispatch_samples,
          count(*) FILTER (WHERE completed_at IS NOT NULL) AS execution_samples
        FROM tool_calls
        WHERE coalesce(completed_at, dispatched_at, created_at) >=
              now() - (%s * interval '1 hour')
        """,
        (hours,),
    ).fetchone()

    spawn = conn.execute(
        """
        WITH ready AS (
            SELECT session_id, (payload ->> 'epoch')::int AS epoch,
                   min(created_at) AS ready_at
              FROM events
             WHERE type = 'sandbox_ready'
               AND created_at >= now() - (%s * interval '1 hour')
             GROUP BY session_id, (payload ->> 'epoch')::int
        ),
        durations AS (
            SELECT extract(epoch FROM (r.ready_at - s.created_at)) AS seconds
              FROM ready r
              JOIN sandboxes s USING (session_id, epoch)
             WHERE r.ready_at >= s.created_at
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds)
                   AS p50_seconds,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds)
                   AS p95_seconds,
               count(*) AS samples
          FROM durations
        """,
        (hours,),
    ).fetchone()

    health = conn.execute(
        """
        SELECT coalesce(max(current_epoch), 0) AS epoch_high_water,
               count(*) FILTER (WHERE status = 'thinking') AS thinking_sessions,
               extract(epoch FROM (
                   now() - min(thinking_since)
               )) AS oldest_thinking_seconds,
               count(*) FILTER (
                   WHERE status = 'executing'
                     AND NOT EXISTS (
                         SELECT 1 FROM tool_calls t
                          WHERE t.session_id = sessions.id
                            AND t.status IN ('pending', 'dispatched')
                     )
               ) AS stalled_executing_sessions
          FROM sessions
        """
    ).fetchone()

    return {
        "generated_at": datetime.now(timezone.utc),
        "window_hours": hours,
        "tiles": {
            "activity": {
                "series": activity,
                "sessions_started": sum(
                    row["sessions_started"] for row in activity
                ),
                "sessions_started_per_hour": (
                    sum(row["sessions_started"] for row in activity) / hours
                ),
                "turns_completed": completed,
                "turns_completed_per_hour": completed / hours,
                "turns_failed": failed,
                "turns_failed_per_hour": failed / hours,
                "completion_rate": completed / outcomes if outcomes else None,
                "definition": (
                    "Completed and failed turns are status outcomes, grouped "
                    "by outcome time."
                ),
            },
            "active_states": {
                "states": [_row_numbers(row) for row in active_states],
                "definition": "Current session rows grouped by status.",
            },
            "turn_duration": {
                "available": False,
                "reason": "Turn boundaries are not persisted yet.",
            },
            "queue": {
                **_row_numbers(queue),
                "definition": (
                    "Exact age covers pending calls that have never been "
                    "dispatched; requeued calls have no pending_since field."
                ),
            },
            "recovery": {
                **recovery,
                "deaths_per_hour": recovery["deaths"] / hours,
                "definition": (
                    "Death-affected sessions that later reached idle, divided "
                    "by resolved recovered or failed sessions."
                ),
            },
            "llm": {
                "available": llm["completed_attempts"] > 0,
                **llm,
                "definition": (
                    "Latency uses successful provider attempts. Attempt errors "
                    "include retries; final-call errors include only terminal outcomes."
                ),
            },
            "cost": {
                "available": cost_available,
                "estimated_cost_usd": llm["estimated_cost_usd"],
                "cost_per_completed_turn_usd": (
                    llm["estimated_cost_usd"] / completed
                    if cost_available
                    else None
                ),
                "priced_attempts": llm["priced_attempts"],
                "successful_attempts": llm["successful_attempts"],
                "input_tokens": llm["input_tokens"],
                "output_tokens": llm["output_tokens"],
                "reason": (
                    None
                    if cost_available
                    else "Need priced token usage and at least one completed turn."
                ),
                "definition": (
                    "Window model cost at the configured grok-4.6 rates, "
                    "divided by completed turn outcomes. Not per-turn attribution."
                ),
            },
            "reexecution": {
                **reexecution,
                "definition": "Calls with attempts > 1 divided by dispatched calls.",
            },
        },
        "secondary": {
            "latency": _row_numbers(latency),
            "spawn": _row_numbers(spawn),
            "loop_health": _row_numbers(health),
        },
    }
