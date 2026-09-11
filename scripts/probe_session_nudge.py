"""Exercise SessionNudge against real rows.

Builds one session per stuck shape plus controls that must not move, runs a
pass, and reports what each scan did. The model client is scripted and the
sandbox runtime is stubbed, so nothing leaves the process.
"""

from __future__ import annotations

import sys
import time
import uuid

sys.path.insert(0, "control")

import config  # noqa: E402
import llm  # noqa: E402
import nudges  # noqa: E402
import sandbox  # noqa: E402
import sessions  # noqa: E402
from db import pool  # noqa: E402

REPO = "/tmp/probe-session-nudge.git"

advanced: list = []


def scripted(context: list):
    """A turn that ends the conversation, so advance() settles at idle."""
    advanced.append(len(context))
    return llm.Reply(text="done")


def fake_runtime(session_id, epoch, repo_url, branch):
    return "fake:{}".format(epoch)


def make(status: str, roles=("system", "user"), thinking_ago=None) -> str:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (repo_url, branch, status) "
                "VALUES (%s, %s, 'idle') RETURNING id",
                (REPO, "agent/" + uuid.uuid4().hex[:8]),
            )
            session_id = str(cur.fetchone()["id"])
            for role in roles:
                sessions._append_message(cur, session_id, role, role + " text")

            # Backdated so the grace period does not hide a fresh row.
            cur.execute(
                "UPDATE sessions SET status = %s, "
                "thinking_since = CASE WHEN %s::float IS NULL THEN NULL "
                "                      ELSE now() - (%s::float * interval '1 second') END, "
                "updated_at = now() - interval '1 hour' "
                "WHERE id = %s",
                (status, thinking_ago, thinking_ago, session_id),
            )
    return session_id


def add_call(session_id: str, status: str) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # tool_calls.message_id is NOT NULL, so the call needs an
            # assistant message to hang off.
            message_id = sessions._append_message(
                cur, session_id, "assistant", "calling a tool"
            )
            cur.execute(
                "INSERT INTO tool_calls (session_id, message_id, provider_call_id, "
                "                        name, args, status, epoch, ordinal) "
                "VALUES (%s, %s, %s, 'read_file', '{}', %s, 1, 0)",
                (session_id, message_id, uuid.uuid4().hex, status),
            )
            # _append_message bumped updated_at, undoing the backdating.
            cur.execute(
                "UPDATE sessions SET updated_at = now() - interval '1 hour' "
                "WHERE id = %s",
                (session_id,),
            )


def state_of(session_id: str) -> tuple:
    """Status, and the assistant message count that proves an advance ran."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT e.status, "
            "       (SELECT count(*) FROM messages m WHERE m.session_id = e.id "
            "         AND m.role = 'assistant') AS turns "
            "  FROM sessions e WHERE e.id = %s",
            (session_id,),
        ).fetchone()
    return row["status"], row["turns"]


def purge() -> int:
    """Drop every session this probe has ever made, including from a run
    that died part-way and left rows behind."""
    with pool.connection() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE repo_url = %s", (REPO,)
        ).rowcount


def main() -> None:
    llm.use(scripted)
    sandbox._start_sandbox = fake_runtime
    config.ADVANCE_GRACE_SECONDS = 0.0
    config.thinking_deadline = lambda: 1.0

    stale = purge()
    if stale:
        print("cleared %s row(s) left by an earlier run" % stale)

    cases = {}

    # -- the three stuck shapes --------------------------------------------
    cases["wake: idle, user last"] = (make("idle", ("system", "user")), "idle")

    wedged = make("thinking", ("system", "user"), thinking_ago=600)
    cases["unwedge: thinking, nothing outstanding"] = (wedged, "idle")

    wedged_busy = make("thinking", ("system", "user"), thinking_ago=600)
    add_call(wedged_busy, "dispatched")
    cases["unwedge: thinking, call outstanding"] = (wedged_busy, "executing")

    stalled = make("executing", ("system", "user", "assistant", "tool"))
    add_call(stalled, "done")
    # Settles at idle: the scripted reply asks for no tools, so the turn ends.
    cases["resume: executing, batch closed"] = (stalled, "idle")

    # -- controls that must not move ---------------------------------------
    cases["control: idle, assistant last"] = (
        make("idle", ("system", "user", "assistant")), "idle",
    )
    cases["control: thinking, inside the bound"] = (
        make("thinking", ("system", "user"), thinking_ago=0), "thinking",
    )
    mid_batch = make("executing", ("system", "user", "assistant"))
    add_call(mid_batch, "pending")
    cases["control: executing, call pending"] = (mid_batch, "executing")
    cases["control: failed"] = (make("failed", ("system", "user")), "failed")
    cases["control: cancelled"] = (make("cancelled", ("system", "user")), "cancelled")

    before = {name: state_of(sid) for name, (sid, _) in cases.items()}

    print()
    print("=== SessionNudge pass ===")
    print(" ", nudges.SessionNudge().run())

    # The hand-off is threaded, so give the advances a moment to land.
    time.sleep(2.0)

    print()
    print("=== per session: status, and turns taken ===")
    for name, (sid, expected) in cases.items():
        status, turns = state_of(sid)
        was_status, was_turns = before[name]
        ran = turns > was_turns
        flag = "ok" if status == expected else "UNEXPECTED (wanted %s)" % expected
        print(
            "  %-42s %-9s -> %-9s  advanced=%-5s  %s"
            % (name, was_status, status, ran, flag)
        )

    print()
    print("model called %s time(s), context sizes %s" % (len(advanced), advanced))

    print()
    print("=== SandboxNudge pass: who gets a sandbox ===")
    print(" ", nudges.SandboxNudge().run())
    # container_id shows provenance. 'fake:' is this probe; anything else came
    # from another instance nudging the same database.
    for name, (sid, _) in cases.items():
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT epoch, status, container_id FROM sandboxes "
                " WHERE session_id = %s ORDER BY epoch",
                (sid,),
            ).fetchall()
        print(
            "  %-42s %s"
            % (
                name,
                [(r["epoch"], r["status"], r["container_id"]) for r in rows]
                or "no sandbox",
            )
        )

    check_handoff_guards()

    print()
    print("deleted %s probe session(s)" % purge())


def check_handoff_guards() -> None:
    """A pass every 15s over advances that take minutes must not pile up.

    Two guards: a session already being advanced here is skipped, and a whole
    pass stops at NUDGE_MAX_ADVANCES. Both are checked with a slow model, so
    the advances are still running when the next pass scans.
    """
    calls: list = []

    def slow(context: list):
        calls.append(time.monotonic())
        time.sleep(1.5)
        return llm.Reply(text="done")

    llm.use(slow)

    print()
    print("=== overlapping passes on one session ===")
    one = make("idle", ("system", "user"))
    for i in range(3):
        print("  pass %s handed off %s" % (i + 1, nudges.SessionNudge().wake_unanswered()))
    time.sleep(2.0)
    print("  model calls for one stuck session: %s (want 1)" % len(calls))

    print()
    print("=== the cap ===")
    calls.clear()
    config.NUDGE_MAX_ADVANCES = 2
    for _ in range(5):
        make("idle", ("system", "user"))
    first = nudges.SessionNudge().wake_unanswered()
    print("  5 stuck sessions, cap 2 -> handed off %s (want 2)" % first)
    time.sleep(2.0)
    second = nudges.SessionNudge().wake_unanswered()
    print("  next pass, slots freed  -> handed off %s (want 2 again)" % second)
    time.sleep(2.0)
    print("  in-flight set drained: %s (want set())" % nudges._advancing)


if __name__ == "__main__":
    main()
