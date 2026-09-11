"""cursord — the daemon inside the sandbox.

    register -> clone -> { heartbeat | poll, execute, checkpoint, report }

It knows nothing about the LLM or the conversation. Every tool call is an
isolated unit of work, and the container holds no state between them that
matters, which is what lets the control plane throw this process away and
rebuild it from the message log.

Two things run at once, and they have to:
  - the heartbeat, every few seconds
  - the work loop, which spends most of its life blocked on a 25s long poll
    or inside a tool call that may run for minutes
A single-threaded version would miss heartbeats for the length of every tool
call and be reaped mid-work, spawning a replacement to redo work that was
about to finish.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import time

from . import config, tools, workspace
from .control import Control, SessionFinished, StaleEpoch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s cursord[e{}] %(levelname)s %(message)s".format(config.EPOCH),
)
logger = logging.getLogger("cursord")


async def heartbeat_loop(control: Control) -> None:
    """Say we are alive until we are told we are not.

    Started before the clone: a large repo can take longer than the death
    threshold, and a sandbox reaped while still cloning never gets to do
    anything.

    A missed beat does not stop the loop and must not stop the process. Only
    a stale epoch ends this container, and only the control plane can say
    that; an unreachable control plane has said nothing at all. Exiting on a
    failed beat would abandon a tool call that is running and about to
    report, and the replacement would have to run it a second time. Deciding
    this sandbox is dead belongs to the reaper, which is the side that can
    still see the clock when the network between us is the thing that broke.
    """
    missed = 0
    while True:
        if await control.heartbeat():
            if missed:
                logger.info("heartbeat landed again after %d missed", missed)
            missed = 0
        else:
            missed += 1
            # Once when it starts, then rarely: this fires every few seconds
            # for as long as the control plane is away, and the log is the
            # only thing anyone has to read afterwards.
            if missed == 1 or missed % 20 == 0:
                logger.warning(
                    "heartbeat missed %d in a row; still working", missed
                )
        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


async def work_loop(control: Control, base: str) -> str:
    """Poll, run, checkpoint, report.

    Returns the reason this container is done, which is only ever that the
    session has been idle too long. Every other ending is a raise: a stale
    epoch, or a session that closed.

    An idle session has finished its turn and is waiting on a person, which
    is a wait with no upper bound. Everything this container holds is
    rebuildable by design — the clone, the packages, the shell — so holding
    it open indefinitely spends a machine on the chance that someone comes
    back. The clock starts on the first idle answer and any work resets it,
    so a conversation that keeps moving never meets it.
    """
    idle_since: float | None = None

    while True:
        poll = await control.next_action()

        if poll.tool is None:
            # The hold expired with nothing pending, which says nothing on
            # its own: a session mid-model-call looks exactly like this.
            if poll.session_status != "idle":
                idle_since = None
                continue

            if idle_since is None:
                idle_since = time.monotonic()
                logger.info(
                    "session is idle; leaving in %.0fs unless it resumes",
                    config.IDLE_EXIT_SECONDS,
                )
            elif time.monotonic() - idle_since >= config.IDLE_EXIT_SECONDS:
                logger.info(
                    "idle for %.0fs; shutting down",
                    time.monotonic() - idle_since,
                )
                return "idle"
            continue

        idle_since = None
        action = poll.tool

        logger.info(
            "action %s %s%s",
            action.name,
            action.action_id[:8],
            " (repeat)" if action.repeated else "",
        )

        result = await tools.execute(action.name, action.args)

        # Push before report, never the other way round. The control plane
        # must never accept a result whose file state is not already durable.
        point = await workspace.checkpoint(
            f"{action.name} {action.action_id[:8]} (epoch {config.EPOCH})",
            base,
        )

        await control.report_result(
            action.action_id,
            result=result.output,
            exit_code=result.exit_code,
            base_sha=base,
            checkpoint=point,
        )


async def main() -> int:
    config.require()

    control = Control()
    heartbeat: asyncio.Task | None = None

    # Why this container stopped, sent as a final beat so the control plane
    # can tell a deliberate shutdown from one that just stopped answering.
    # None means say nothing and let the reaper draw its own conclusion.
    #
    # It starts at "crashed" so that an exception nobody anticipated is
    # reported as one; every path below that knows better overwrites it.
    farewell: str | None = "crashed"
    try:
        registration = await control.register(container_id=_container_id())
        logger.info(
            "registered on %s, resuming from %s",
            registration.branch,
            (registration.resume_sha or "base")[:12],
        )

        heartbeat = asyncio.create_task(heartbeat_loop(control))

        head = await workspace.prepare(
            registration.repo_url, registration.branch, registration.resume_sha
        )
        logger.info("workspace ready at %s", head[:12])

        # On a first spawn the control plane has no base_sha and no way to
        # get one: it holds no clone. This container does, so the base is
        # whatever it just cloned, and it travels up with the first result.
        # A rebuild takes the base from registration instead, because `head`
        # here is the point it resumed from, not where the work began.
        base = registration.base_sha or head

        work = asyncio.create_task(work_loop(control, base))

        # Whichever finishes first finishes the process. The heartbeat only
        # ever ends by raising; the work loop can also return, which is what
        # an idle session looks like from here.
        done, pending = await asyncio.wait(
            {heartbeat, work}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise

        farewell = work.result() if work in done else "stopped"
        return 0

    except StaleEpoch as exc:
        # A newer sandbox owns this session. Anything still in flight here is
        # already worthless, and the branch will be reset past whatever we
        # pushed. Leave quietly: a beat at a stale epoch is refused anyway,
        # and the control plane is the side that decided this.
        farewell = None
        logger.warning("epoch %s is stale (%s); shutting down", config.EPOCH, exc)
        return 0
    except SessionFinished as exc:
        # The one shutdown that is not a failure, and the only reason the
        # control plane treats as clean.
        farewell = "session_finished"
        logger.info("session %s; shutting down", exc)
        return 0
    except asyncio.CancelledError:
        farewell = "cancelled"
        return 0
    finally:
        # Before the goodbye, so a beat still in flight cannot land after it
        # and put this sandbox back in the reaper's index.
        if heartbeat is not None:
            heartbeat.cancel()
        if farewell is not None:
            # A container killed outright never reaches this, which is
            # correct: that sandbox is dead rather than exiting, and the
            # reaper is the thing that notices.
            await control.report_exit(farewell)
        await control.aclose()


def _container_id() -> str:
    """Docker sets the hostname to the short container id unless told otherwise."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
