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
import log_context
import sandbox
import tools

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
@log_context.correlated
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


@log_context.correlated
def add_user_message(session_id: str, content: str) -> dict:
    """Append a user message, and start the loop again if it is idle.

    Appending and advancing are separate: a message landing while calls are
    outstanding must not trigger a turn, because the provider expects tool
    results to follow the assistant message that asked for them. It is in the
    log either way, so whichever request advances next picks it up.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Locked so the status cannot change underneath the append.
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
        # A session idle long enough loses its sandbox, so restore the
        # invariant before asking for work. Ordered the way create_session
        # orders it, so the container boots while the model thinks.
        sandbox.ensure(session_id)
        advance(session_id)
        return {"advanced": True}

    # thinking or executing: something else is already running and will see
    # this message when it reloads the context.
    return {"advanced": False}


@log_context.correlated
def get_events(
    session_id: str,
    after: int = 0,
    limit: int = 500,
    timeout: float = config.LONG_POLL_SECONDS,
) -> dict:
    """Long-poll the UI feed for events after a sequence number.

    No events means the hold expired; next_after comes back unchanged so the
    caller can poll again with what it has.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Up front, so a bad id fails now rather than after the full hold.
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



@log_context.correlated
def get_diff(session_id: str) -> dict:
    """What changed, in summary, and where to read the rest.

    A pure read. The summary was computed in the sandbox and stored with the
    SHA it describes. The full patch stays in the repository; `url` links to it.
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
        # Empty rather than absent, so the client renders without branching.
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

    # scp-style (git@host:owner/repo) and URL forms both reduce to host + path.
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
@log_context.correlated
def advance(session_id: str) -> None:
    """One iteration of the inner loop.

    A no-op if another instance is already advancing this session, or if the
    session is finished.
    """
    if not _claim_thinking(session_id):
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
            context = _load_context(cur, session_id)
            # Where the log stood when this context was built, so a message
            # that lands during the call can be told from one already in it.
            sent_through = _message_seq(cur, session_id)

    # Outside any transaction: this can take minutes and must hold no locks.
    try:
        reply = llm.complete(context, session_id=session_id)
    except Exception as exc:
        # Recorded on the session so the client hears about it on the feed.
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

                # A message that arrived during the call is not in the context
                # that produced this answer, so it is still unanswered.
                # Compared against the seq, not the last role: the assistant
                # row above is already appended and would always be last.
                unanswered = _user_message_after(cur, session_id, sent_through)
            else:
                unanswered = False
                # The index is the dispatch order, and cursord runs one call at
                # a time, so it is also the execution order.
                for ordinal, call in enumerate(reply.tool_calls):
                    action_id = _insert_tool_call(
                        cur, session_id, message_id, call, ordinal
                    )
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


@log_context.correlated
def claim_next_action(
    session_id: str, epoch: int, timeout: float = config.LONG_POLL_SECONDS
) -> dict:
    """Long-poll for the next tool call.

    Always reports the session status, because a null tool alone cannot
    distinguish "still thinking" from "there will never be another call".
    """
    deadline = time.monotonic() + timeout
    while True:
        status, action = _claim_one_action(session_id, epoch)
        if action is not None:
            return {"session_status": status, "tool": action}

        # No hold: there is nothing to wait for, so let the sandbox stop.
        if status in TERMINAL_STATUSES:
            return {"session_status": status, "tool": None}

        if time.monotonic() >= deadline:
            return {"session_status": status, "tool": None}
        time.sleep(config.POLL_INTERVAL_SECONDS)


@log_context.correlated
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

    A non-zero exit code is still a result: it reaches the model as content
    rather than failing the call.

    `schedule` runs the advance after the caller has answered its own client.
    A turn is unbounded and cursord's read timeout is 35s, so advancing inline
    would make it give up and retry a result already accepted. Runs inline
    when no scheduler is given.
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

            # Spelled out, not handed the row: this payload is the client's
            # contract, and the row holds uuids json.dumps cannot take.
            _emit(
                cur,
                session_id,
                "tool_finished",
                {
                    "action_id": str(row["action_id"]),
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
                # Parallel calls: only the last result of the batch advances.
                return {"batch_complete": False}

            # Batch is in. One tool message each, so the context replays
            # against the provider's tool_call ids.
            cur.execute(
                "SELECT provider_call_id, name, result, exit_code, repeated, "
                "attempts, status "
                "FROM tool_calls WHERE session_id = %s AND message_id = %s "
                "ORDER BY ordinal",
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

    # Outside the transaction, so the rows the model reads are committed first.
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


def _message_seq(cur, session_id: str) -> int:
    """The session's message counter, as a high-water mark for the log."""
    cur.execute("SELECT message_seq FROM sessions WHERE id = %s", (session_id,))
    row = cur.fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    return row["message_seq"]


def _user_message_after(cur, session_id: str, seq: int) -> bool:
    cur.execute(
        "SELECT 1 FROM messages WHERE session_id = %s AND role = 'user' "
        "AND seq > %s LIMIT 1",
        (session_id, seq),
    )
    return cur.fetchone() is not None


def _load_context(cur, session_id: str) -> list[dict]:
    """Rebuild the provider-shaped message array from the log.

    A user message that lands mid-batch is held back until the batch closes.
    The provider requires the tool replies to an assistant message to follow
    it immediately, so a user row appended between them would be rejected as
    a malformed conversation. Only the array is reordered; the log keeps every
    message at the point it actually arrived.
    """
    cur.execute(
        "SELECT role, content, tool_calls, provider_call_id "
        "FROM messages WHERE session_id = %s ORDER BY seq",
        (session_id,),
    )

    out = []
    held: list[dict] = []
    owed = 0  # tool replies the last assistant message is still waiting on

    for row in cur.fetchall():
        msg: dict[str, Any] = {"role": row["role"]}
        if row["content"] is not None:
            msg["content"] = row["content"]
        if row["tool_calls"]:
            msg["tool_calls"] = row["tool_calls"]
        if row["provider_call_id"]:
            msg["tool_call_id"] = row["provider_call_id"]

        if owed and row["role"] == "user":
            held.append(msg)
            continue

        out.append(msg)

        if row["role"] == "assistant" and row["tool_calls"]:
            owed = len(row["tool_calls"])
        elif owed and row["role"] == "tool":
            owed -= 1
            if not owed:
                out.extend(held)
                held.clear()

    # An unfinished batch, which advance is not normally entered with. The
    # held messages still belong in the array rather than dropped from it.
    out.extend(held)
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


def rescue_orphaned_calls(cur, session_id: str) -> list:
    """Hand a session's in-flight tool calls back to whoever comes next.

    A call dispatched to a dead sandbox is stuck, because _claim_one_action
    only selects 'pending'. Returning it to 'pending' is the whole recovery.

    Only rows behind the current epoch; at the current epoch they are in
    flight. Results from the dead epoch are refused by record_action_result's
    epoch guard whether or not this ran.

    attempts, epoch and dispatched_at are left alone: attempts is what keeps
    MAX_TOOL_ATTEMPTS a real ceiling and sets `repeated` on re-dispatch, and
    the other two record where the lost attempt went.
    """
    cur.execute(
        """
        UPDATE tool_calls SET status = 'pending'
         WHERE session_id = %s
           AND status = 'dispatched'
           AND epoch < (SELECT current_epoch FROM sessions WHERE id = %s)
        RETURNING id, name, epoch, attempts, ordinal
        """,
        (session_id, session_id),
    )
    rescued = cur.fetchall()
    for call in rescued:
        _emit(
            cur,
            session_id,
            "tool_requeued",
            {
                "action_id": str(call["id"]),
                "name": call["name"],
                "lost_epoch": call["epoch"],
                "attempts": call["attempts"],
            },
        )
    return rescued


def _insert_tool_call(cur, session_id: str, message_id: str, call, ordinal: int) -> str:
    cur.execute(
        """
        INSERT INTO tool_calls (session_id, message_id, provider_call_id, name,
                                args, ordinal)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            session_id,
            message_id,
            call.provider_call_id,
            call.name,
            json.dumps(call.args),
            ordinal,
        ),
    )
    return cur.fetchone()["id"]


def _claim_thinking(session_id: str) -> bool:
    """Win the right to call the model.

    Its own short transaction, so no lock is held across the network call.
    Losing is normal: another instance is advancing, or the session is done.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Parameterised, not inlined: a status that stopped matching here
            # fails silently, leaving a session that never thinks again.
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
    """Status and epoch for a caller that is the live sandbox.

    One read, so the status a caller acts on was true when its epoch passed.

    Equality, not "no older than". An epoch above current_epoch is not a
    sandbox that fell behind, it is one that cannot exist, because the only
    thing that opens an epoch is a spawn. Accepting it would let a caller
    claim work and report results against an epoch nothing ever created.
    """
    cur.execute(
        "SELECT status, current_epoch FROM sessions WHERE id = %s", (session_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise SessionNotFound(session_id)
    if epoch != row["current_epoch"]:
        raise StaleEpoch(
            f"epoch {epoch} is not current, session {session_id} is at "
            f"epoch {row['current_epoch']}"
        )
    return row


def _require_current_epoch(cur, session_id: str, epoch: int) -> int:
    """Reject a caller from a sandbox that has already been replaced."""
    return _require_current(cur, session_id, epoch)["current_epoch"]


def _claim_one_action(session_id: str, epoch: int) -> tuple[str, dict | None]:
    """Hand the oldest pending call to exactly one caller.

    Returns the session status too, so the caller can tell "nothing pending
    yet" from "nothing will ever be pending". SKIP LOCKED lets two sandboxes
    polling at once take different rows. RETURNING names columns the way the
    sandbox reads them, so the claimed row is the response body.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            status = _require_current(cur, session_id, epoch)["status"]
            if status in TERMINAL_STATUSES:
                # A cancelled session can still hold pending rows, and their
                # results would never be accepted.
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
                        -- created_at ties across a batch (it defaults to the
                        -- transaction clock), so ordinal breaks it.
                        ORDER BY created_at, ordinal
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
                # Not the status read at entry: this call made it terminal, and
                # the sandbox should exit on this response, not poll again.
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
        # The only case that is a 404.
        return UnknownAction(f"no tool call {action_id} on session {session_id}")
    if row["epoch"] is not None and row["epoch"] != epoch:
        return StaleEpoch(
            f"tool call {action_id} went to epoch {row['epoch']}, not {epoch}"
        )
    # Exists and belongs to this caller, just not in a state that takes a
    # result: a conflict, not a missing resource.
    return ActionNotDispatched(
        f"tool call {action_id} is {row['status']}, expected dispatched"
    )


def _tool_message(call: dict) -> str:
    """What the model sees for one completed tool call.

    Execution is at-least-once, and the control plane cannot tell whether a
    lost attempt ran. Rather than guess, it hands the model the facts and
    lets it judge: it is the only party that knows what the command was for
    and what a duplicate of it would mean.

    The standing policy — what rolls back, what does not, and what to do — is
    in the system prompt, stated once. What goes here is only the facts of
    this incident, because this text is prepended to a result the model is
    reading for its content.
    """
    audit = call["name"] in tools.AUDIT_ON_REPEAT

    if call["status"] == "failed":
        note = f"tool call did not complete after {call['attempts']} attempts"
        if audit:
            # Every attempt was dispatched to a sandbox, so every one of them
            # may have run before the sandbox it went to died.
            note += (
                "; each attempt reached a sandbox, so effects outside the "
                "workspace may have landed more than once"
            )
        return note

    body = call["result"] or ""
    if call["repeated"] and audit:
        body = (
            f"[repeat] attempt {call['attempts']}: the sandbox running the "
            "previous attempt died and its output was not kept, so this "
            "command may have executed more than once.\n" + body
        )
    if call["exit_code"]:
        body = f"{body}\n[exit code {call['exit_code']}]"
    return body


def _preview(text: str | None) -> str | None:
    if text is None or len(text) <= config.RESULT_PREVIEW_CHARS:
        return text
    dropped = len(text) - config.RESULT_PREVIEW_CHARS
    return text[:config.RESULT_PREVIEW_CHARS] + f"\n… truncated, {dropped} more characters"
