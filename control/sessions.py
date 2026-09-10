from __future__ import annotations

import json
import uuid
from typing import Any

from psycopg.rows import dict_row

import sandbox
from db import pool


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


# ---------- ---------- ----------
# client-facing API
# ---------- ---------- ----------
def create_session(repo_url: str, prompt:str) -> dict:
    branch = f"agent/{uuid.uuid4().hex[:8]}"
    with pool.connection() as conn:
        conn.row_factory = dict_row
        with conn.cursor(row_factory=dict_row) as cur:
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

