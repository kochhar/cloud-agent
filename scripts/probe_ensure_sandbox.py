"""Check the sandbox invariant at the points that enforce it.

Covers sandbox.ensure directly, add_user_message on a session whose sandbox
has left, and the loss ceiling now that an ordinary resume opens an epoch.
The model client is scripted and the runtime stubbed, so nothing leaves the
process.
"""

from __future__ import annotations

import sys
import uuid

sys.path.insert(0, "control")

import config  # noqa: E402
import llm  # noqa: E402
import sandbox  # noqa: E402
import sessions  # noqa: E402
from db import pool  # noqa: E402

REPO = "/tmp/probe-ensure.git"


def scripted(context: list):
    return llm.Reply(text="done")


def fake_runtime(session_id, epoch, repo_url, branch):
    return "fake:{}".format(epoch)


def purge() -> int:
    with pool.connection() as conn:
        return conn.execute(
            "DELETE FROM sessions WHERE repo_url = %s", (REPO,)
        ).rowcount


def new_session(prompt: str = "do the thing") -> str:
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (repo_url, branch, status) "
                "VALUES (%s, %s, 'idle') RETURNING id",
                (REPO, "agent/" + uuid.uuid4().hex[:8]),
            )
            session_id = str(cur.fetchone()["id"])
            sessions._append_message(cur, session_id, "system", "sys")
            sessions._append_message(cur, session_id, "user", prompt)
    return session_id


def set_sandbox(session_id: str, epoch: int, status: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO sandboxes (session_id, epoch, container_id, status, "
            "                       last_heartbeat_at) "
            "VALUES (%s, %s, 'fake:preexisting', %s, now()) "
            "ON CONFLICT (session_id, epoch) DO UPDATE SET status = EXCLUDED.status",
            (session_id, epoch, status),
        )


def snapshot(session_id: str) -> dict:
    with pool.connection() as conn:
        session = conn.execute(
            "SELECT status, current_epoch FROM sessions WHERE id = %s",
            (session_id,),
        ).fetchone()
        boxes = conn.execute(
            "SELECT epoch, status FROM sandboxes WHERE session_id = %s "
            "ORDER BY epoch",
            (session_id,),
        ).fetchall()
    return {
        "status": session["status"],
        "epoch": session["current_epoch"],
        "sandboxes": [(b["epoch"], b["status"]) for b in boxes],
    }


def main() -> None:
    llm.use(scripted)
    sandbox._start_sandbox = fake_runtime

    stale = purge()
    if stale:
        print("cleared %s row(s) from an earlier run" % stale)

    print()
    print("=== ensure: is it a no-op when a sandbox is live? ===")
    for status in ("spawning", "ready", "exited", "dead"):
        session_id = new_session()
        with pool.connection() as conn:
            conn.execute(
                "UPDATE sessions SET current_epoch = 1 WHERE id = %s", (session_id,)
            )
        set_sandbox(session_id, 1, status)
        made = sandbox.ensure(session_id)
        print(
            "  row at current epoch is %-9s -> %s"
            % (status, "spawned epoch %s" % made["epoch"] if made else "no-op")
        )

    print()
    print("=== ensure: nothing to spawn against ===")
    for name, status in (("failed", "failed"), ("cancelled", "cancelled")):
        session_id = new_session()
        with pool.connection() as conn:
            conn.execute(
                "UPDATE sessions SET status = %s WHERE id = %s", (status, session_id)
            )
        try:
            sandbox.ensure(session_id)
            print("  %-9s session -> spawned (UNEXPECTED)" % name)
        except sandbox.SpawnRefused as exc:
            print("  %-9s session -> refused: %s" % (name, exc))

    print()
    print("=== add_user_message on a session whose sandbox has left ===")
    session_id = new_session()
    with pool.connection() as conn:
        conn.execute(
            "UPDATE sessions SET current_epoch = 1 WHERE id = %s", (session_id,)
        )
    set_sandbox(session_id, 1, "exited")
    print("  before:", snapshot(session_id))
    sessions.add_user_message(session_id, "and another thing")
    print("  after: ", snapshot(session_id))

    print()
    print("=== the loss ceiling counts deaths, not epochs ===")
    config.MAX_SANDBOX_LOSSES = 3

    resumed = new_session()
    for i in range(6):
        # Each resume ends cleanly, the way an idle sandbox leaves.
        sandbox.ensure(resumed)
        with pool.connection() as conn:
            conn.execute(
                "UPDATE sandboxes SET status = 'exited' WHERE session_id = %s",
                (resumed,),
            )
    state = snapshot(resumed)
    print(
        "  6 clean resumes -> epoch %s, session %s (want epoch 6, idle)"
        % (state["epoch"], state["status"])
    )

    crashed = new_session()
    sandbox.spawn(crashed)
    for i in range(6):
        try:
            with pool.connection() as conn:
                conn.execute(
                    "UPDATE sessions SET status = 'executing' WHERE id = %s",
                    (crashed,),
                )
            sandbox.spawn(
                crashed,
                expect_epoch=snapshot(crashed)["epoch"],
                reason="heartbeat_expired",
            )
        except sandbox.SpawnRefused as exc:
            print("  death %s refused: %s" % (i + 1, exc))
            break
    state = snapshot(crashed)
    print("  after the ceiling -> session %s, epoch %s" % (state["status"], state["epoch"]))

    print()
    print("deleted %s probe session(s)" % purge())


if __name__ == "__main__":
    main()
