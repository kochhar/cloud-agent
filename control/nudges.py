"""Periodic scans that unstick sessions nothing else will move.

Every nudge is idempotent, epoch-guarded and a no-op on a healthy session, so
all instances can run the same loop with no leader election. See
docs/agent-nudger.md.
"""

from __future__ import annotations

import logging
import threading

import config
import log_context
import sandbox
import sessions
from db import pool
from models import EXECUTING, IDLE, THINKING

logger = logging.getLogger(__name__)

# 'idle' is excluded: a replacement would poll, find nothing and leave.
WANTS_SANDBOX = (EXECUTING, THINKING)


class Nudge:
    """One family of scans over one kind of stuck state."""

    name = "nudge"

    def run(self) -> dict:
        raise NotImplementedError


class SandboxNudge(Nudge):
    """Replace sandboxes that are not coming back. Never matches 'exited'."""

    name = "sandbox"

    def run(self) -> dict:
        # spawn_for_pending last: it keys off having no live row, and the
        # scans before it are what produce one.
        return {
            "expired": self.reap_expired(),
            "announced_dead": self.reap_announced_dead(),
            "stuck_spawning": self.reap_stuck_spawning(),
            "spawned_for_work": self.spawn_for_pending(),
        }

    def reap_expired(self) -> int:
        """'ready', but the heartbeats stopped."""
        return self._replace(
            self._candidates(
                "s.status = 'ready' "
                "AND s.last_heartbeat_at < now() - (%s * interval '1 second')",
                (config.HEARTBEAT_DEATH_SECONDS,),
            ),
            "heartbeat_expired",
        )

    def reap_announced_dead(self) -> int:
        """'dead', reported by cursord. Its heartbeat is fresh, so no timed
        scan would find it."""
        return self._replace(
            self._candidates("s.status = 'dead'", ()),
            "reported_dead",
        )

    def reap_stuck_spawning(self) -> int:
        """'spawning', and the runtime never came back. _start_sandbox returns
        None rather than raising, so the row is neither 'ready' nor 'dead'."""
        return self._replace(
            self._candidates(
                "s.status = 'spawning' "
                "AND s.created_at < now() - (%s * interval '1 second')",
                (config.SPAWN_STUCK_SECONDS,),
            ),
            "spawn_timeout",
        )

    def spawn_for_pending(self) -> int:
        """Pending work and no sandbox left to run it.

        A backstop for sandbox.ensure, and the only scan that starts from the
        absence of a row rather than from one.
        """
        with pool.connection() as conn:
            stranded = conn.execute(
                "SELECT e.id, e.current_epoch "
                "  FROM sessions e "
                " WHERE e.status = %s "
                "   AND EXISTS (SELECT 1 FROM tool_calls t "
                "                WHERE t.session_id = e.id "
                "                  AND t.status = 'pending') "
                "   AND NOT EXISTS (SELECT 1 FROM sandboxes s "
                "                    WHERE s.session_id = e.id "
                "                      AND s.epoch = e.current_epoch "
                "                      AND s.status = ANY(%s))",
                (EXECUTING, list(sandbox.LIVE_STATUSES)),
            ).fetchall()

        spawned = 0
        for row in stranded:
            session_id = str(row["id"])
            with log_context.bind(session_id=session_id, epoch=row["current_epoch"]):
                try:
                    # No reason: nothing died, so the feed gets no crash.
                    fresh = sandbox.spawn(
                        session_id, expect_epoch=row["current_epoch"]
                    )
                except sandbox.SpawnRefused as exc:
                    logger.info("no sandbox for session %s: %s", session_id, exc)
                    continue

                spawned += 1
                logger.warning(
                    "session %s had pending work and no sandbox; spawned epoch %s",
                    session_id,
                    fresh["epoch"],
                )
        return spawned

    # -- plumbing ----------------------------------------------------------

    def _candidates(self, predicate: str, params: tuple) -> list:
        """Sandboxes matching `predicate` whose session still wants one.

        Deliberately unlocked: spawn's compare-and-swap is what makes
        replacement exactly-once.
        """
        with pool.connection() as conn:
            return conn.execute(
                "SELECT s.session_id, s.epoch, s.container_id, s.status "
                "  FROM sandboxes s "
                "  JOIN sessions e ON e.id = s.session_id "
                " WHERE s.epoch = e.current_epoch "
                "   AND e.status = ANY(%s) "
                "   AND " + predicate,
                (list(WANTS_SANDBOX),) + params,
            ).fetchall()

    def _replace(self, rows: list, reason: str) -> int:
        """Spawn a replacement for each at a new epoch.

        Outside any transaction of ours: spawn opens its own and starts a
        process.
        """
        replaced = 0
        for row in rows:
            session_id = str(row["session_id"])
            epoch = row["epoch"]
            # Per session, since a pass spans many.
            with log_context.bind(session_id=session_id, epoch=epoch):
                try:
                    spawned = sandbox.spawn(
                        session_id, expect_epoch=epoch, reason=reason
                    )
                except sandbox.SpawnRefused as exc:
                    logger.info("no replacement for session %s: %s", session_id, exc)
                    continue

                replaced += 1
                logger.warning(
                    "replaced sandbox for session %s: epoch %s was %s (%s), "
                    "now epoch %s",
                    session_id,
                    epoch,
                    row["status"],
                    reason,
                    spawned["epoch"],
                )
        return replaced


class ToolCallNudge(Nudge):
    """Hand in-flight tool calls to the sandbox that replaced theirs.

    A backstop for the rescue inside spawn, which covers every replacement it
    makes but not a claim that committed after the rescue had looked.
    """

    name = "tool_call"

    def run(self) -> dict:
        return {"rescued": self.rescue_orphaned()}

    def rescue_orphaned(self) -> int:
        """Requeue every 'dispatched' call whose epoch is behind its session."""
        with pool.connection() as conn:
            stuck = conn.execute(
                "SELECT DISTINCT t.session_id "
                "  FROM tool_calls t "
                "  JOIN sessions e ON e.id = t.session_id "
                " WHERE t.status = 'dispatched' "
                "   AND t.epoch < e.current_epoch "
                "   AND e.status = ANY(%s)",
                (list(WANTS_SANDBOX),),
            ).fetchall()

        rescued = 0
        for row in stuck:
            session_id = str(row["session_id"])
            with log_context.bind(session_id=session_id):
                with pool.connection() as conn:
                    with conn.cursor() as cur:
                        calls = sessions.rescue_orphaned_calls(cur, session_id)
                if calls:
                    rescued += len(calls)
                    logger.warning(
                        "rescued %s orphaned call(s) for session %s: %s",
                        len(calls),
                        session_id,
                        ", ".join(
                            "%s (epoch %s, attempt %s)"
                            % (c["name"], c["epoch"], c["attempts"])
                            for c in calls
                        ),
                    )
        return rescued


class SessionNudge(Nudge):
    """Take the turn nobody is left to finish.

    Each scan ends in advance(), which is safe from any instance because
    _claim_thinking is a guarded UPDATE.
    """

    name = "session"

    def run(self) -> dict:
        return {
            "woken": self.wake_unanswered(),
            "unwedged": self.unwedge_thinking(),
            "resumed": self.resume_stalled_batch(),
        }

    def wake_unanswered(self) -> int:
        """'idle' with a user message last, so nothing ever answered it."""
        return self._hand_off(
            self._sessions(
                "e.status = %s "
                "AND e.updated_at < now() - (%s * interval '1 second') "
                "AND (SELECT role FROM messages m WHERE m.session_id = e.id "
                "      ORDER BY seq DESC LIMIT 1) = 'user'",
                (IDLE, config.ADVANCE_GRACE_SECONDS),
            ),
            "unanswered_message",
        )

    def unwedge_thinking(self) -> int:
        """'thinking' for longer than a turn can take, so the caller is gone.

        Returns the number released, which is the repair: 'thinking' is the one
        status advance() will not claim. The turn itself is lost rather than
        half-written, since the model call runs in no transaction.

        Only the ones left idle are advanced. Advancing a session with live
        tool calls would put a second batch in front of a model still waiting
        on the first; its sandbox is working, and the batch closing advances it.
        """
        wedged = self._sessions(
            "e.status = %s "
            "AND e.thinking_since < now() - (%s * interval '1 second')",
            (THINKING, config.thinking_deadline()),
        )

        released = [(s, self._release(s)) for s in wedged]
        idle = [s for s, status in released if status == IDLE]
        self._hand_off(idle, "thinking_expired")
        return sum(1 for _, status in released if status is not None)

    def resume_stalled_batch(self) -> int:
        """'executing' with nothing outstanding, so the batch closed unnoticed.

        The last result committed and the advance it scheduled never ran. A
        retry from the sandbox cannot fix it: the result was already accepted.
        """
        return self._hand_off(
            self._sessions(
                "e.status = %s "
                "AND e.updated_at < now() - (%s * interval '1 second') "
                "AND NOT EXISTS (SELECT 1 FROM tool_calls t "
                "                 WHERE t.session_id = e.id "
                "                   AND t.status IN ('pending','dispatched'))",
                (EXECUTING, config.ADVANCE_GRACE_SECONDS),
            ),
            "stalled_batch",
        )

    # -- plumbing ----------------------------------------------------------

    def _sessions(self, predicate: str, params: tuple) -> list:
        with pool.connection() as conn:
            rows = conn.execute(
                "SELECT e.id FROM sessions e WHERE " + predicate, params
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def _release(self, session_id: str) -> str | None:
        """Put a wedged session back to the status its tool calls imply.

        The UPDATE repeats the scan's predicate as a compare-and-swap, so an
        instance that claimed the session in between keeps it. Returns the new
        status, or None if this caller did not free it.
        """
        with log_context.bind(session_id=session_id):
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    try:
                        row = sessions._lock_session(cur, session_id)
                    except sessions.SessionNotFound:
                        return None
                    if row["status"] != THINKING:
                        return None

                    cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM tool_calls "
                        " WHERE session_id = %s "
                        "   AND status IN ('pending','dispatched')) AS outstanding",
                        (session_id,),
                    )
                    status = EXECUTING if cur.fetchone()["outstanding"] else IDLE

                    cur.execute(
                        "UPDATE sessions SET status = %s, thinking_since = NULL, "
                        "updated_at = now() "
                        " WHERE id = %s AND status = %s "
                        "   AND thinking_since < now() - (%s * interval '1 second') "
                        "RETURNING id",
                        (status, session_id, THINKING, config.thinking_deadline()),
                    )
                    if cur.fetchone() is None:
                        return None

                    sessions._emit(cur, session_id, "status", {"status": status})

            logger.warning(
                "session %s was wedged in %s for over %.0fs; released to %s",
                session_id,
                THINKING,
                config.thinking_deadline(),
                status,
            )
            return status

    def _hand_off(self, session_ids: list, reason: str) -> int:
        """Start each advance on its own thread, and do not wait.

        advance() runs the model, so inline it would stall the pass and the
        startup sweep, which runs before the server accepts traffic. Daemon
        threads: an abandoned advance is what unwedge_thinking recovers.
        """
        handed = 0
        for session_id in session_ids:
            if not _reserve(session_id):
                continue
            with log_context.bind(session_id=session_id):
                logger.warning("advancing session %s: %s", session_id, reason)
            threading.Thread(
                target=_advance,
                args=(session_id,),
                name="advance-{}".format(session_id[:8]),
                daemon=True,
            ).start()
            handed += 1
        return handed


# Advances this instance is running, and the cap on them. Not a correctness
# guard — _claim_thinking is — but a scan runs every interval while an advance
# takes minutes, so without it a backlog grows a thread per pass.
# The cap is per process: NUDGE_MAX_ADVANCES = 8 with three instances is 24
# concurrent advances, all racing _claim_thinking.
_advancing: set = set()
_advancing_lock = threading.Lock()


def _reserve(session_id: str) -> bool:
    """Take a slot on this process. The cap is not cluster-wide."""
    with _advancing_lock:
        if session_id in _advancing:
            return False
        if len(_advancing) >= config.NUDGE_MAX_ADVANCES:
            return False
        _advancing.add(session_id)
        return True


def _advance(session_id: str) -> None:
    # Bound inside the thread: a new one starts with an empty context, so a
    # binding around the hand-off would not reach here.
    with log_context.bind(session_id=session_id):
        try:
            try:
                sandbox.ensure(session_id)
            except sandbox.SpawnRefused as exc:
                # Either the session takes no work and advance declines it
                # too, or someone else spawned and the invariant holds.
                logger.info("no sandbox for session %s: %s", session_id, exc)

            sessions.advance(session_id)
        except Exception:
            # Nothing is waiting on this thread, and advance records model
            # failures on the session itself.
            logger.exception("advance failed for session %s", session_id)
        finally:
            with _advancing_lock:
                _advancing.discard(session_id)


# Sandboxes first, since replacing one rescues its calls; sessions last, so
# they act on what the others put right. Latency only, not correctness.
NUDGES = (SandboxNudge(), ToolCallNudge(), SessionNudge())


def run_once() -> dict:
    """One pass of every nudge, for /internal/reap and the startup sweep.

    Caught per nudge, so one bad session cannot stop recovery for the rest.
    """
    report = {}
    for nudge in NUDGES:
        try:
            report[nudge.name] = nudge.run()
        except Exception as exc:
            logger.exception("nudge %s failed", nudge.name)
            report[nudge.name] = {"error": str(exc)}
    return report


def run_forever(stop: threading.Event) -> None:
    """The periodic pass, for a daemon thread."""
    logger.info("nudger started, every %ss", config.NUDGE_INTERVAL_SECONDS)
    while not stop.wait(config.NUDGE_INTERVAL_SECONDS):
        report = run_once()
        # A pass that moved nothing is the common case.
        if any(
            count
            for scans in report.values()
            for count in (scans.values() if isinstance(scans, dict) else ())
        ):
            logger.info("nudger pass: %s", report)
    logger.info("nudger stopped")
