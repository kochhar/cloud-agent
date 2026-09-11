from __future__ import annotations

import json
import logging
import time
from typing import Any
import uuid

from db import pool
from models import TERMINAL_STATUSES
import config
import llm
import sandbox

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    pass


class StaleEpoch(Exception):
    pass


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


def get_events(
    session_id: str,
    after: int = 0,
    limit: int = 500,
    timeout: float = config.LONG_POLL_SECONDS,
) -> list[dict]:
    """Long-poll the UI feed for events after a sequence number.

    An empty list means the hold expired and the client should poll again with
    the same cursor.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Check once up front, so polling a bad id fails now rather than
            # after the full hold.
            cur.execute("SELECT 1 FROM sessions WHERE id = %s", (session_id,))
            if cur.fetchone() is None:
                raise SessionNotFound(session_id)

    deadline = time.monotonic() + timeout
    while True:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, type, payload, created_at FROM events "
                    "WHERE session_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                    (session_id, after, limit),
                )
                rows = cur.fetchall()
        
        if rows:
            return rows
        if time.monotonic() >= deadline:
            return []
        time.sleep(config.POLL_INTERVAL_SECONDS)


# ---------- ---------- ----------
# the loop
# ---------- ---------- ----------
def advance(session_id: str) -> None:
    """One iteration of the inner loop.

    Does nothing if another instance is already advancing this session, or 
    if the session is finished.
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


# ---------- ---------- ----------
# sandbox-facing API
# ---------- ---------- ----------

class UnknownAction(Exception):
    pass


def claim_next_action(
    session_id: str, epoch: int, timeout: float = config.LONG_POLL_SECONDS
) -> dict:
    """Long-poll for the next tool call.

    Always reports the session status. A null tool on its own is ambiguous —
    it means both "the model is still thinking" and "there will never be
    another call" — and a sandbox that cannot tell those apart polls a
    finished session forever.
    """
    deadline = time.monotonic() + timeout
    while True:
        status, action = _claim_one_action(session_id, epoch)
        if action is not None:
            return {"tool": action, "session_status": status}

        # Returned without waiting out the hold: there is nothing to wait for,
        # and the sooner the sandbox hears this the sooner it stops.
        if status in TERMINAL_STATUSES:
            return {"tool": None, "session_status": status}

        if time.monotonic() >= deadline:
            return {"tool": None, "session_status": status}
        time.sleep(config.POLL_INTERVAL_SECONDS)


def record_action_result(
    session_id: str,
    action_id: str,
    epoch: int,
    result: str | None = None,
    exit_code: int | None = None,
    commit_sha: str | None = None,
) -> bool:
    """Accept a tool result from the current epoch.

    Returns True when this was the last outstanding call of its batch, which
    is the caller's cue to advance the loop. A non-zero exit code is still a
    result: it goes back to the model as content rather than failing the call.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _require_current_epoch(cur, session_id, epoch)

            cur.execute(
                """
                UPDATE tool_calls
                   SET status = 'done',
                       result = %s,
                       exit_code = %s,
                       commit_sha = %s,
                       completed_at = now()
                 WHERE id = %s
                   AND session_id = %s
                   AND status = 'dispatched'
                   AND epoch = (SELECT current_epoch FROM sessions WHERE id = %s)
                RETURNING id, name, message_id, exit_code, commit_sha, repeated
                """,
                (result, exit_code, commit_sha, action_id, session_id, session_id),
            )
            row = cur.fetchone()
            if row is None:
                raise _rejected(cur, session_id, action_id, epoch)

            if commit_sha:
                # Only a SHA from a live epoch may move the branch head.
                cur.execute(
                    "UPDATE sessions SET last_accepted_sha = %s, updated_at = now() "
                    "WHERE id = %s",
                    (commit_sha, session_id),
                )

            _emit(
                cur,
                session_id,
                "tool_finished",
                {
                    "action_id": str(row["id"]),
                    "name": row["name"],
                    "exit_code": row["exit_code"],
                    "commit_sha": row["commit_sha"],
                    "repeated": row["repeated"],
                    "result": _preview(result),
                },
            )

            cur.execute(
                "SELECT count(*) AS open FROM tool_calls "
                "WHERE session_id = %s AND message_id = %s "
                "AND status IN ('pending', 'dispatched')",
                (session_id, row["message_id"]),
            )
            if cur.fetchone()["open"]:
                return False

            # Whole batch is in. Every result becomes its own tool message so
            # the context replays against the provider's tool_call ids.
            cur.execute(
                "SELECT provider_call_id, result, exit_code, repeated, attempts, status "
                "FROM tool_calls WHERE session_id = %s AND message_id = %s "
                "ORDER BY created_at",
                (session_id, row["message_id"]),
            )
            for call in cur.fetchall():
                _append_message(
                    cur,
                    session_id,
                    "tool",
                    content=_tool_message(call),
                    provider_call_id=call["provider_call_id"],
                )
            return True


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


def _require_current(cur, session_id: str, epoch: int) -> dict:
    """Status and epoch for a caller that is still the live sandbox.

    Both come from one read so the status a caller acts on is the status that
    was true when its epoch was checked.
    """
    cur.execute(
        "SELECT status, current_epoch FROM sessions WHERE id = %s", (session_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    if epoch < row["current_epoch"]:
        raise StaleEpoch(
            f"epoch {epoch} is stale, session {session_id} is at "
            f"epoch {row['current_epoch']}"
        )
    return row


def _require_current_epoch(cur, session_id: str, epoch: int) -> int:
    """Reject a caller from a sandbox that has already been replaced."""
    return _require_current(cur, session_id, epoch)["current_epoch"]


def _claim_one_action(session_id: str, epoch: int) -> tuple[str, dict | None]:
    """Hand the oldest pending call to exactly one caller.

    Returns the session status alongside the claim, because the caller has to
    tell "nothing pending yet" apart from "nothing will ever be pending".

    SKIP LOCKED means two sandboxes polling at the same moment take different
    rows instead of blocking on each other.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            status = _require_current(cur, session_id, epoch)["status"]
            if status in TERMINAL_STATUSES:
                # A cancelled session can still have pending rows. Claiming
                # one would hand out work whose result will never be accepted.
                return status, None

            cur.execute(
                """
                UPDATE tool_calls
                   SET status = 'dispatched',
                       epoch = %s,
                       attempts = attempts + 1,
                       repeated = (attempts > 0),
                       dispatched_at = now()
                 WHERE id = (
                       SELECT id FROM tool_calls
                        WHERE session_id = %s
                          AND status = 'pending'
                        ORDER BY created_at
                        LIMIT 1
                          FOR UPDATE SKIP LOCKED)
                RETURNING id, name, args, attempts, repeated
                """,
                (epoch, session_id),
            )
            row = cur.fetchone()
            if row is None:
                return status, None

            if row["attempts"] > config.MAX_TOOL_ATTEMPTS:
                cur.execute(
                    "UPDATE tool_calls SET status = 'failed', completed_at = now() "
                    "WHERE id = %s",
                    (row["id"],),
                )
                _set_status(
                    cur,
                    session_id,
                    "failed",
                    f"tool call {row['name']} failed after {row['attempts'] - 1} attempts",
                )
                # Reported as failed rather than as the status read at entry:
                # this call is what made it terminal, and the sandbox should
                # exit on this response rather than poll once more to find out.
                return "failed", None

            return status, row


def _rejected(cur, session_id: str, action_id: str, epoch: int) -> Exception:
    """Turn a no-op UPDATE into the reason it did not apply."""
    cur.execute(
        "SELECT status, epoch FROM tool_calls WHERE id = %s AND session_id = %s",
        (action_id, session_id),
    )
    row = cur.fetchone()
    if row is None:
        return UnknownAction(f"no tool call {action_id} on session {session_id}")
    if row["epoch"] is not None and row["epoch"] != epoch:
        return StaleEpoch(
            f"tool call {action_id} went to epoch {row['epoch']}, not {epoch}"
        )
    return UnknownAction(
        f"tool call {action_id} is {row['status']}, expected dispatched"
    )


def _tool_message(call: dict) -> str:
    """What the model sees for one completed tool call."""
    if call["status"] == "failed":
        return f"tool call did not complete after {call['attempts']} attempts"

    body = call["result"] or ""
    if call["repeated"]:
        # At-least-once execution: the model should know this may have run twice.
        body = (
            "[this call was re-dispatched after a sandbox died and may have "
            "executed more than once]\n" + body
        )
    if call["exit_code"]:
        body = f"{body}\n[exit code {call['exit_code']}]"
    return body


def _preview(text: str | None) -> str | None:
    if text is None or len(text) <= config.RESULT_PREVIEW_CHARS:
        return text
    dropped = len(text) - config.RESULT_PREVIEW_CHARS
    return text[:config.RESULT_PREVIEW_CHARS] + f"\n… truncated, {dropped} more characters"
