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
    """
    while True:
        await control.heartbeat()
        await asyncio.sleep(config.HEARTBEAT_INTERVAL)


async def work_loop(control: Control) -> None:
    """Poll, run, checkpoint, report. Forever, or until the epoch turns over."""
    while True:
        action = await control.next_action()
        if action is None:
            continue  # hold expired with nothing pending; poll again

        logger.info(
            "action %s %s%s",
            action.name,
            action.action_id[:8],
            " (repeat)" if action.repeated else "",
        )

        result = await tools.execute(action.name, action.args)

        # Push before report, never the other way round. The control plane
        # must never accept a result whose file state is not already durable.
        sha = await workspace.checkpoint(
            f"{action.name} {action.action_id[:8]} (epoch {config.EPOCH})"
        )

        await control.report_result(
            action.action_id,
            result=result.output,
            exit_code=result.exit_code,
            commit_sha=sha,
        )


async def main() -> int:
    config.require()

    control = Control()
    heartbeat: asyncio.Task | None = None
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
        # TODO(contract): on a first spawn the control plane has no base_sha
        # until someone resolves the default branch, and the container is the
        # only party holding a clone. Either register reports `head` back, or
        # the spawner resolves it with `git ls-remote` against the bare repo
        # before the container exists. The second is better: it means
        # base_sha is known even if no sandbox ever starts.

        work = asyncio.create_task(work_loop(control))

        # Whichever finishes first finishes the process. In practice that is
        # always a raise: neither loop returns on its own.
        done, pending = await asyncio.wait(
            {heartbeat, work}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise
        return 0

    except StaleEpoch as exc:
        # A newer sandbox owns this session. Anything still in flight here is
        # already worthless, and the branch will be reset past whatever we
        # pushed. Leave quietly.
        logger.warning("epoch %s is stale (%s); shutting down", config.EPOCH, exc)
        return 0
    except SessionFinished as exc:
        logger.info("session %s; shutting down", exc)
        return 0
    except asyncio.CancelledError:
        return 0
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
        await control.aclose()


def _container_id() -> str:
    """Docker sets the hostname to the short container id unless told otherwise."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
