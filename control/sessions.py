from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable
import uuid

from db import pool
from models import EXECUTING, FAILED, IDLE, THINKING, TERMINAL_STATUSES
import config
import llm
import sandbox

logger = logging.getLogger(__name__)


class SessionNotFound(Exception):
    pass


class SessionFinished(Exception):
    """The session reached a terminal state and takes no more input."""


class StaleEpoch(Exception):
    pass


# ---------- ---------- ----------
# client-facing API
# ---------- ---------- ----------
def create_session(repo_url: str, prompt:str) -> dict:
    """Create a new session, spawn a container and advance the loop."""
    branch = f"agent/{uuid.uuid4().hex[:8]}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (repo_url, branch, status) "
                "VALUES (%s, %s, %s) RETURNING id, branch",
                (repo_url, branch, IDLE),
            )

            row = cur.fetchone()
            
            session_id = str(row["id"])
            _append_message(cur, session_id, "system", llm.SYSTEM_PROMPT)
            _append_message(cur, session_id, "user", prompt)
            _emit(cur, session_id, "status", {"status": "starting"})
    
    # Container boot and the first LLM call happen in parallel.
    sandbox.spawn(session_id)
    advance(session_id)
    return {"session_id": session_id, "branch": branch}


def add_user_message(session_id: str, content: str) -> dict:
    """Append a user message, and start the loop again if it is idle.

    Appending and advancing are separate. A message that lands while calls 
    are outstanding must not trigger a model because the model expects the 
    tool results to follow the assistant message that asked for them.
    
    The message is in the log either way, so whichever request advances the 
    loop next picks it up.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Locked FOR UPDATE so the status cannot change underneath the append.
            cur.execute(
                "SELECT status FROM sessions WHERE id = %s FOR UPDATE", (session_id,)
            )
        
            row = cur.fetchone()
            if row is None:
                raise SessionNotFound(session_id)

            status = row["status"]
            if status in TERMINAL_STATUSES:
                raise SessionFinished(
                    f"session {session_id} is {status} and takes no more messages"
                )

            _append_message(cur, session_id, "user", content)

    if status == IDLE:
        advance(session_id)
        return {"advanced": True}

    # thinking or executing: something else is already running and will see
    # this message when it reloads the context.
    return {"advanced": False}


def get_events(
    session_id: str,
    after: int = 0,
    limit: int = 500,
    timeout: float = config.LONG_POLL_SECONDS,
) -> dict:
    """Long-poll the UI feed for events after a sequence number.

    No events means the hold expired, and next_after comes back unchanged so
    the caller can poll again with what it already has.
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
            return {"events": rows, "next_after": rows[-1]["seq"]}
        if time.monotonic() >= deadline:
            return {"events": [], "next_after": after}
        time.sleep(config.POLL_INTERVAL_SECONDS)



def get_diff(session_id: str) -> dict:
    """What changed, in summary, and where to read the rest of it.

    A read, diff was computed in the sandbox,  and written alongside the SHA 
    it describes when that result was accepted.

    The full patch is not here. It lives in the repository the sandbox pushed to, 
    and `url` is how a client gets to it.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT repo_url, branch, base_sha, last_accepted_sha, "
                "diff_preview, diff_stat, status FROM sessions WHERE id = %s",
                (session_id,),
            )
            row = cur.fetchone()

    if row is None:
        raise SessionNotFound(session_id)

    base = row["base_sha"]
    head = row["last_accepted_sha"]
    stat = row["diff_stat"] or {}

    return {
        "branch": row["branch"],
        "status": row["status"],
        "base_sha": base,
        "head_sha": head,
        # No accepted commit means no sandbox has reported one: Empty, not 
        # absent, so client can render without branching.
        "files": stat.get("files", []),
        "files_changed": stat.get("files_changed", 0),
        "additions": stat.get("additions", 0),
        "deletions": stat.get("deletions", 0),
        "files_truncated": stat.get("files_truncated", False),
        "preview": row["diff_preview"] or "",
        "preview_truncated": stat.get("preview_truncated", False),
        "url": _compare_url(row["repo_url"], base, head),
    }


def _compare_url(repo_url: str, base: str | None, head: str | None) -> str | None:
    """A link to the full patch on the forge hosting the repo."""
    if not base or not head or base == head:
        return None

    # scp-style (git@host:owner/repo) and URL forms both reduce to a host and
    # a path, and neither has a scheme worth keeping.
    remainder = repo_url.split("://", 1)[-1].rsplit("@", 1)[-1]
    host, separator, path = remainder.partition(":" if ":" in remainder.split("/")[0] else "/")
    if not separator:
        return None

    template = config.COMPARE_URLS.get(host)
    if template is None:
        return None
    return template.format(repo=path.strip("/").removesuffix(".git"), base=base, head=head)


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

    # Deliberately outside transaction: this can take a minute, and nothing 
    # should sit locked while it runs.
    try:
        reply = llm.complete(context)
    except Exception as exc:
        # Recorded on the session, so the handler still returns and the client 
        # learns about it from the event feed.
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
                _set_status(cur, session_id, IDLE)

                # A user message which arrived while we were in the model call
                # is not answered. add_user_message saw a busy session and left 
                # the nudge to us to handle after the call finishes.
                unanswered = _last_message_role(cur, session_id) == "user"
            else:
                unanswered = False
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
                _set_status(cur, session_id, EXECUTING)

    if unanswered:
        advance(session_id)


# ---------- ---------- ----------
# sandbox-facing API
# ---------- ---------- ----------

class UnknownAction(Exception):
    """No such tool call on this session."""


class ActionNotDispatched(Exception):
    """The tool call exists but is not waiting on a result."""


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
            return {"session_status": status, "tool": action}

        # Returned without waiting out the hold: there is nothing to wait for,
        # and the sooner the sandbox hears this the sooner it stops.
        if status in TERMINAL_STATUSES:
            return {"session_status": status, "tool": None}

        if time.monotonic() >= deadline:
            return {"session_status": status, "tool": None}
        time.sleep(config.POLL_INTERVAL_SECONDS)


def record_action_result(
    session_id: str,
    action_id: str,
    epoch: int,
    result: str | None = None,
    exit_code: int | None = None,
    commit_sha: str | None = None,
    base_sha: str | None = None,
    diff_preview: str | None = None,
    diff_stat: dict | None = None,
    schedule: Callable[..., None] | None = None,
) -> dict:
    """Accept a tool result from the current epoch, and advance if it was the last.

    A non-zero exit code is still a result: it goes back to the model as
    content rather than failing the call.

    `schedule` is how the caller runs work after it has answered its own
    client. A model turn is unbounded and cursord's read timeout is 35s, 
    so advancing inside the request makes it give up and retry a result 
    that was already accepted. 
    
    Called inline when no scheduler is offered, which is what direct callers 
    and tests want.
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
                RETURNING id AS action_id, name, message_id, exit_code, commit_sha, repeated AS repeated
                """,
                (result, exit_code, commit_sha, action_id, session_id, session_id),
            )
            row = cur.fetchone()
                     
            if row is None:
                if _already_accepted(cur, session_id, action_id, epoch):
                    logger.info(
                        "duplicate result for tool call %s on session %s, "
                        "already accepted",
                        action_id,
                        session_id,
                    )
                    return {"batch_complete": False}
                raise _rejected(cur, session_id, action_id, epoch)

            cur.execute(
                "UPDATE sessions SET base_sha = COALESCE(base_sha, %s), "
                "updated_at = now() WHERE id = %s",
                (base_sha, session_id),
            )
            if commit_sha:
                cur.execute(
                    "UPDATE sessions SET last_accepted_sha = %s, "
                    "diff_preview = %s, diff_stat = %s, updated_at = now() "
                    "WHERE id = %s",
                    (
                        commit_sha,
                        (diff_preview or "")[: config.DIFF_PREVIEW_CHARS],
                        json.dumps(diff_stat or {}),
                        session_id,
                    ),
                )

            row["result"] = _preview(result)
            _emit(cur, session_id, "tool_finished", row)

            cur.execute(
                "SELECT count(*) AS open FROM tool_calls "
                "WHERE session_id = %s AND message_id = %s "
                "AND status IN ('pending', 'dispatched')",
                (session_id, row["message_id"]),
            )
            if cur.fetchone()["open"]:
                # Parallel calls: only the last result of the batch advances.
                return {"batch_complete": False}

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

    # Outside the transaction: the rows the model is about to read are
    # committed before anything goes looking for them.
    _defer(schedule, advance, session_id)
    return {"batch_complete": True}


def _defer(schedule: Callable[..., None] | None, function, *args) -> None:
    """Hand work to the caller's scheduler, or just do it here."""
    if schedule is None:
        function(*args)
    else:
        schedule(function, *args)


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


def _last_message_role(cur, session_id: str) -> str | None:
    cur.execute(
        "SELECT role FROM messages WHERE session_id = %s ORDER BY seq DESC LIMIT 1",
        (session_id,),
    )
    row = cur.fetchone()
    return row["role"] if row else None


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
        "thinking_since = CASE WHEN %s = %s THEN now() ELSE NULL END, "
        "updated_at = now() WHERE id = %s",
        (status, error, status, THINKING, session_id),
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
            # Parameterised rather than inlined because this is the one place
            # a status that stopped matching would be silent: no row, no
            # error, and advance() quietly becomes a session that never
            # thinks again.
            cur.execute(
                "UPDATE sessions SET status = %s, thinking_since = now(), "
                "updated_at = now() "
                "WHERE id = %s AND status IN (%s, %s) "
                "RETURNING id",
                (THINKING, session_id, IDLE, EXECUTING),
            )
            if cur.fetchone() is None:
                return False
            _emit(cur, session_id, "status", {"status": THINKING})
            return True


def _fail(session_id: str, error: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            _set_status(cur, session_id, FAILED, error)


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

    The RETURNING clause names the columns the way the sandbox reads them, so
    the claimed row is the response body rather than something to copy into
    one.
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
                RETURNING id AS action_id, name, args, attempts AS attempt, repeated
                """,
                (epoch, session_id),
            )
            row = cur.fetchone()
            if row is None:
                return status, None

            if row["attempt"] > config.MAX_TOOL_ATTEMPTS:
                cur.execute(
                    "UPDATE tool_calls SET status = 'failed', completed_at = now() "
                    "WHERE id = %s",
                    (row["action_id"],),
                )
                _set_status(
                    cur,
                    session_id,
                    FAILED,
                    f"tool call {row['name']} failed after {row['attempt'] - 1} attempts",
                )
                # Reported as failed rather than as the status read at entry:
                # this call is what made it terminal, and the sandbox should
                # exit on this response rather than poll once more to find out.
                return FAILED, None

            return status, row


def _already_accepted(cur, session_id: str, action_id: str, epoch: int) -> bool:
    """Is this a retry of a result that was already taken from this epoch?"""
    cur.execute(
        "SELECT 1 FROM tool_calls WHERE id = %s AND session_id = %s "
        "AND status = 'done' AND epoch = %s",
        (action_id, session_id, epoch),
    )
    return cur.fetchone() is not None


def _rejected(cur, session_id: str, action_id: str, epoch: int) -> Exception:
    """Turn a no-op UPDATE into the reason it did not apply."""
    cur.execute(
        "SELECT status, epoch FROM tool_calls WHERE id = %s AND session_id = %s",
        (action_id, session_id),
    )
    row = cur.fetchone()
    if row is None:
        # Genuinely nothing here: the only case that is a 404.
        return UnknownAction(f"no tool call {action_id} on session {session_id}")
    if row["epoch"] is not None and row["epoch"] != epoch:
        return StaleEpoch(
            f"tool call {action_id} went to epoch {row['epoch']}, not {epoch}"
        )
    # The call exists and belongs to this caller; it is just not in a state
    # that can take a result. That is a conflict with the current state, not
    # a missing resource.
    return ActionNotDispatched(
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
