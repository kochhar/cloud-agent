from __future__ import annotations

import json
import logging
from typing import Any
import uuid

from db import pool
import llm
import sandbox

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    pass


class StaleEpoch(Exception):
    pass


def _next_seq(cur, session_id: str, column: str) -> int:
    cur.execute(
        f"UPDATE sessions SET {column} = {column} + 1, updated_at = now() "
        "WHERE id = %s RETURNING " + column,
        (session_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    return row[column]


def _emit(cur, session_id: str, kind: str, payload: dict[str, Any]) -> None:
    seq = _next_seq(cur, session_id, "event_seq")
    cur.execute(
        "INSERT INTO events (session_id, seq, type, payload) VALUES (%s, %s, %s, %s)",
        (session_id, seq, kind, json.dumps(payload)),
    )


def _append_message(
    cur,
    session_id: str,
    role: str,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict] | None = None,
    provider_call_id: str | None = None,
) -> str:
    seq = _next_seq(cur, session_id, "message_seq")
    cur.execute(
        """
        INSERT INTO messages
            (session_id, seq, role, content, reasoning, tool_calls, provider_call_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            session_id,
            seq,
            role,
            content,
            reasoning,
            json.dumps(tool_calls) if tool_calls else None,
            provider_call_id,
        ),
    )
    return cur.fetchone()["id"]


def _load_context(cur, session_id: str) -> list[dict]:
    """Rebuild the provider-shaped message array from the log."""
    cur.execute(
        "SELECT role, content, tool_calls, provider_call_id "
        "FROM messages WHERE session_id = %s ORDER BY seq",
        (session_id,),
    )
    
    out = []
    for row in cur.fetchall():
        msg: dict[str, Any] = {"role": row["role"]}
        if row["content"] is not None:
            msg["content"] = row["content"]
        if row["tool_calls"]:
            msg["tool_calls"] = row["tool_calls"]
        if row["provider_call_id"]:
            msg["tool_call_id"] = row["provider_call_id"]
        out.append(msg)
    return out


def _current_epoch(cur, session_id: str) -> int:
    cur.execute("SELECT current_epoch FROM sessions WHERE id = %s", (session_id,))
    row = cur.fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    return row["current_epoch"]


def _set_status(cur, session_id: str, status: str, error: str | None = None) -> None:
    cur.execute(
        "UPDATE sessions SET status = %s, error = %s, "
        "thinking_since = CASE WHEN %s = 'thinking' THEN now() ELSE NULL END, "
        "updated_at = now() WHERE id = %s",
        (status, error, status, session_id),
    )
    payload = {"status": status}
    if error:
        payload["error"] = error
    _emit(cur, session_id, "status", payload)


def _insert_tool_call(cur, session_id: str, message_id: str, call) -> str:
    cur.execute(
        """
        INSERT INTO tool_calls (session_id, message_id, provider_call_id, name, args)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            session_id,
            message_id,
            call.provider_call_id,
            call.name,
            json.dumps(call.args),
        ),
    )
    return cur.fetchone()["id"]


def _claim_thinking(session_id: str) -> bool:
    """Win the right to call the model.

    Its own short transaction, so the row is not held locked across a network
    call. Losing the race is normal: it means another instance is already
    advancing this session, or the session is finished.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sessions SET status = 'thinking', thinking_since = now(), "
                "updated_at = now() "
                "WHERE id = %s AND status IN ('awaiting_user', 'executing') "
                "RETURNING id",
                (session_id,),
            )
            if cur.fetchone() is None:
                return False
            _emit(cur, session_id, "status", {"status": "thinking"})
            return True


def _fail(session_id: str, error: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _set_status(cur, session_id, "failed", error)


# ---------- ---------- ----------
# client-facing API
# ---------- ---------- ----------
def create_session(repo_url: str, prompt:str) -> dict:
    branch = f"agent/{uuid.uuid4().hex[:8]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (repo_url, branch, status) "
                "VALUES (%s, %s, 'awaiting_user') RETURNING id, branch",
                (repo_url, branch),
            )

            row = cur.fetchone()
            session_id = row["id"]
            _append_message(cur, session_id, "system", llm.SYSTEM_PROMPT)
            _append_message(cur, session_id, "user", prompt)
            _emit(cur, session_id, "status", {"status": "starting"})
    
    # Container boot and the first LLM call happen in parallel.
    sandbox.spawn(session_id)
    advance(session_id)
    return {"session_id": session_id, "branch": branch}


# ---------- ---------- ----------
# the loop
# ---------- ---------- ----------
def advance(session_id: str) -> None:
    """One iteration of the inner loop.

    Runs when a request arrives that gives it something to do: a user message,
    or the last outstanding tool result. Does nothing if another instance is
    already advancing this session, or if the session is finished.
    """
    if not _claim_thinking(session_id):
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            context = _load_context(cur, session_id)

    # Deliberately outside any transaction: this is a network call that can
    # take a minute, and nothing should sit locked while it runs.
    try:
        reply = llm.complete(context)
    except Exception as exc:
        # Recorded on the session rather than raised, so the handler that
        # triggered this still returns and the client learns about it from
        # the event feed.
        logger.exception("model call failed for session %s", session_id)
        _fail(session_id, f"model call failed: {exc}")
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            message_id = _append_message(
                cur,
                session_id,
                "assistant",
                content=reply.text,
                reasoning=reply.reasoning,
                tool_calls=reply.raw_tool_calls,
            )

            if reply.text:
                _emit(cur, session_id, "text", {"text": reply.text})
            if reply.reasoning:
                _emit(cur, session_id, "thinking", {"text": reply.reasoning})

            if not reply.tool_calls:
                # The doc is inconsistent here: the loop pseudocode says
                # awaiting_user, the Lifecycle section says complete. Following
                # the pseudocode so the client can keep the conversation going.
                _set_status(cur, session_id, "awaiting_user")
                return

            for call in reply.tool_calls:
                action_id = _insert_tool_call(cur, session_id, message_id, call)
                _emit(
                    cur,
                    session_id,
                    "tool_started",
                    {
                        "action_id": str(action_id),
                        "name": call.name,
                        "args": call.args,
                    },
                )
            _set_status(cur, session_id, "executing")
