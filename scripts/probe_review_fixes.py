"""Check the four review findings.

1. every sandbox-facing operation requires the exact current epoch
2. a user message landing mid-batch cannot split an assistant/tool pair
3. no code path emits sandbox_replaced
4. a message arriving during the model call is noticed when it ends
"""

from __future__ import annotations

import json
import sys
import uuid

sys.path.insert(0, "control")

import llm  # noqa: E402
import sandbox  # noqa: E402
import sessions  # noqa: E402
from db import pool  # noqa: E402

REPO = "/tmp/probe-review.git"


def purge() -> int:
    with pool.connection() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE repo_url = %s", (REPO,)
        ).rowcount


def new_session(epoch: int = 1) -> str:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (repo_url, branch, status, current_epoch) "
                "VALUES (%s, %s, 'idle', %s) RETURNING id",
                (REPO, "agent/" + uuid.uuid4().hex[:8], epoch),
            )
            session_id = str(cur.fetchone()["id"])
            sessions._append_message(cur, session_id, "system", "sys")
            cur.execute(
                "INSERT INTO sandboxes (session_id, epoch, container_id, status, "
                "                       last_heartbeat_at) "
                "VALUES (%s, %s, 'fake:1', 'ready', now())",
                (session_id, epoch),
            )
    return session_id


def batch(session_id: str, names: list, answered: int) -> None:
    """An assistant turn asking for len(names) tools, `answered` of them back."""
    calls = [
        {
            "id": "call_%s" % i,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for i, name in enumerate(names)
    ]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            sessions._append_message(
                cur, session_id, "assistant", content=None, tool_calls=calls
            )
            for i in range(answered):
                sessions._append_message(
                    cur,
                    session_id,
                    "tool",
                    content="result %s" % i,
                    provider_call_id="call_%s" % i,
                )


def roles(session_id: str) -> list:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            return [m["role"] for m in sessions._load_context(cur, session_id)]


def stored_roles(session_id: str) -> list:
    with pool.connection() as conn:
        return [
            r["role"]
            for r in conn.execute(
                "SELECT role FROM messages WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
        ]


def valid(order: list) -> bool:
    """Every assistant tool_calls block is answered before anything else."""
    owed = 0
    for role in order:
        if owed and role != "tool":
            return False
        if role == "tool":
            owed -= 1
        elif role == "assistant_calls":
            owed = 0  # set by the caller's encoding
    return owed == 0


def main() -> None:
    stale = purge()
    if stale:
        print("cleared %s row(s) from an earlier run" % stale)

    print("=== 1. epoch validation ===")
    session_id = new_session(epoch=1)
    for epoch, label in ((0, "behind"), (1, "current"), (2, "ahead")):
        results = []
        for name, call in (
            ("heartbeat", lambda: sandbox.heartbeat(session_id, epoch)),
            ("register", lambda: sandbox.register(session_id, epoch, "fake:1")),
            ("claim", lambda: sessions.claim_next_action(session_id, epoch)),
        ):
            try:
                call()
                results.append("%s=ACCEPTED" % name)
            except Exception as exc:
                results.append("%s=%s" % (name, type(exc).__name__))
        print("  epoch %s (%-7s) %s" % (epoch, label, "  ".join(results)))

    print()
    print("=== 2. a user message landing mid-batch ===")
    session_id = new_session()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            sessions._append_message(cur, session_id, "user", "first ask")
    # Two tools asked for, one answered, then the user speaks, then the rest.
    batch(session_id, ["read_file", "run_command"], answered=1)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            sessions._append_message(cur, session_id, "user", "actually, wait")
            sessions._append_message(
                cur, session_id, "tool", content="result 1",
                provider_call_id="call_1",
            )
    print("  stored order: %s" % stored_roles(session_id))
    print("  context order: %s" % roles(session_id))

    order = roles(session_id)
    split = any(
        order[i] == "user" and order[i - 1] == "tool" and order[i + 1] == "tool"
        for i in range(1, len(order) - 1)
    )
    tool_run_intact = order[-2:] == ["tool", "user"] or "user" not in order[2:-1]
    print("  user row splits the tool replies: %s (want False)" % split)
    print("  held message still present:      %s" % (order.count("user") == 2))

    print()
    print("=== 3. sandbox_replaced ===")
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) AS n FROM events WHERE type = 'sandbox_replaced'"
        ).fetchone()
    print("  rows in events: %s" % rows["n"])
    print("  emitted anywhere in control/: see the grep below")

    print()
    print("=== 4. a message arriving during the model call ===")
    session_id = new_session()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            sessions._append_message(cur, session_id, "user", "start")

    turns = []

    def interrupting(context: list):
        turns.append([m["role"] for m in context])
        if len(turns) == 1:
            # The user speaks while this first call is in flight.
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    sessions._append_message(cur, session_id, "user", "one more thing")
        return llm.Reply(text="answer %s" % len(turns))

    llm.use(interrupting)
    sandbox._start_sandbox = lambda s, e, r, b: "fake:%s" % e
    sessions.advance(session_id)
    print("  model turns: %s" % len(turns))
    for i, context in enumerate(turns, 1):
        print("    turn %s context: %s" % (i, context))
    print("  second turn happened without a nudge: %s" % (len(turns) == 2))

    print()
    print("deleted %s probe session(s)" % purge())


if __name__ == "__main__":
    main()
