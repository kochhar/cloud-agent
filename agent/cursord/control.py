"""The only thing in the container that talks to the control plane.

Every request carries the epoch the container was born with. A 409 means the
session has moved on to a newer sandbox and this process is a zombie: it stops
work immediately rather than finishing the call it is holding, because nothing
it produces from here on will be accepted.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from . import config

logger = logging.getLogger(__name__)


class StaleEpoch(Exception):
    """This container has been replaced. Unwind and exit."""


class SessionFinished(Exception):
    """The session reached a terminal state. Nothing left to do."""


@dataclass(frozen=True)
class Registration:
    """What the control plane tells us at registration.

    `resume_sha` is the point the workspace must be forced to, which is the
    last SHA the control plane accepted from a live epoch. On a first spawn it
    equals the base. On a rebuild it is wherever the dead sandbox got to
    before its last accepted result, which is not necessarily the branch tip:
    a sandbox that pushed and then died before reporting left a commit nobody
    accepted, and we discard it.
    """

    repo_url: str
    branch: str
    base_sha: Optional[str]
    resume_sha: Optional[str]


@dataclass(frozen=True)
class Action:
    """One tool call, dispatched to exactly one sandbox."""

    action_id: str
    name: str
    args: dict[str, Any]
    attempt: int
    repeated: bool


@dataclass(frozen=True)
class Poll:
    """One answer to the long poll.

    A null tool means only that nothing was pending when the hold expired,
    which is the ordinary case while the model is thinking. What the sandbox
    does about that depends on `session_status`, which is why the status is
    handed back rather than swallowed here: 'executing' and 'idle' both
    arrive with no tool, and only one of them is a reason to go home.
    """

    session_status: Optional[str]
    tool: Optional[Action]


class Control:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=config.CONTROL_URL,
            timeout=httpx.Timeout(10.0, read=config.POLL_READ_TIMEOUT),
        )
        self._base = f"/sandbox/{config.SESSION_ID}"

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- lifecycle ---------------------------------------------------------

    async def register(self, container_id: Optional[str] = None) -> Registration:
        """Announce this epoch and find out where to start from.

        Retried until it succeeds: the container can easily win the race
        against the control plane instance that spawned it, and a connection
        refused here is a timing artifact rather than a failure.
        """
        body = await self._retrying(
            "register",
            lambda: self._http.post(
                f"{self._base}/register",
                json={"epoch": config.EPOCH, "container_id": container_id},
            ),
        )
        return Registration(
            repo_url=body.get("repo_url", config.REPO_URL),
            branch=body.get("branch", config.BRANCH),
            base_sha=body.get("base_sha"),
            resume_sha=body.get("resume_sha") or body.get("base_sha"),
        )

    async def heartbeat(self) -> bool:
        """Liveness. True if the beat landed.

        A 409 raises StaleEpoch: a newer sandbox owns the session, and that
        is the one heartbeat failure that means something. Every other
        failure — a control plane restarting, a 5xx, a timeout — is reported
        back rather than raised, because the caller is a loop and taking the
        container down over it would throw away work in flight.

        Not routed through `_retrying` on purpose. A heartbeat is only worth
        anything at the moment it is sent; resending a stale one says nothing
        the next tick will not say better, and a retry that outlasts the
        interval would stall the beat it is standing in for.
        """
        try:
            await self._request(
                self._http.post(f"{self._base}/heartbeat", json={"epoch": config.EPOCH})
            )
        except httpx.HTTPError as exc:
            logger.debug("heartbeat failed: %s", exc)
            return False
        return True

    async def report_exit(self, reason: str) -> None:
        """The last beat: this container is stopping, and why.

        Without it a deliberate shutdown is indistinguishable from a crash,
        and the reaper spends its death threshold waiting to replace a
        sandbox that finished on purpose.

        Best effort, and never retried. The process is already leaving, and
        anything that failed here is something the reaper will work out from
        the heartbeat that stops arriving. That includes a stale epoch: the
        control plane has replaced us, which is exactly what we were about
        to tell it.
        """
        try:
            await self._request(
                self._http.post(
                    f"{self._base}/heartbeat",
                    json={
                        "epoch": config.EPOCH,
                        "exiting": True,
                        "reason": reason,
                    },
                )
            )
        except (httpx.HTTPError, StaleEpoch) as exc:
            logger.debug("exit report failed: %s", exc)

    # -- work --------------------------------------------------------------

    async def next_action(self) -> Poll:
        """Long-poll for the pending tool call.

        'failed' and 'cancelled' raise here rather than being returned: the
        session is closed, nothing further can be accepted from this
        container, and there is no policy left for the caller to apply.
        'idle' is not one of them — the session can still be resumed by a
        user message, so how long to wait for one is the work loop's call.
        """
        body = await self._retrying(
            "next-action",
            lambda: self._http.get(
                f"{self._base}/next-action", params={"epoch": config.EPOCH}
            ),
        )

        status = body.get("session_status")
        if status in {"failed", "cancelled"}:
            raise SessionFinished(status)

        tool = body.get("tool")
        return Poll(
            session_status=status,
            tool=None if tool is None else Action(
                action_id=tool["action_id"],
                name=tool["name"],
                args=tool.get("args") or {},
                attempt=tool.get("attempt", 1),
                repeated=tool.get("repeated", False),
            ),
        )

    async def report_result(
        self,
        action_id: str,
        *,
        result: str,
        exit_code: Optional[int],
        base_sha: Optional[str] = None,
        checkpoint=None,
    ) -> None:
        """Hand back a tool result. This is the call that advances the loop.

        Retried hard, because the work is already done and the commit is
        already pushed: giving up here is what turns a completed tool call
        into a duplicated one after recovery.

        The checkpoint rides along because the control plane has no clone and
        no way to look any of this up for itself. `base_sha` goes with every
        result, commit or not, so the session knows where its work started
        from the first one rather than from the first write.
        """
        await self._retrying(
            "result",
            lambda: self._http.post(
                f"{self._base}/actions/{action_id}/result",
                json={
                    "epoch": config.EPOCH,
                    "result": result,
                    "exit_code": exit_code,
                    "base_sha": base_sha,
                    "commit_sha": checkpoint.sha if checkpoint else None,
                    "diff_preview": checkpoint.preview if checkpoint else None,
                    "diff_stat": checkpoint.stat if checkpoint else None,
                },
            ),
        )

    # -- plumbing ----------------------------------------------------------

    async def _request(self, awaitable) -> dict:
        response = await awaitable
        if response.status_code == 409:
            raise StaleEpoch(_detail(response))
        response.raise_for_status()
        return response.json()

    async def _retrying(self, what: str, build) -> dict:
        """Retry transport failures forever, but never retry a stale epoch.

        There is no attempt ceiling on purpose. The control plane going away
        is a restart, not an outage, and the reaper on the other side is what
        decides this container has lived too long.
        """
        delay = config.RETRY_BASE_SECONDS
        while True:
            try:
                return await self._request(build())
            except (StaleEpoch, SessionFinished):
                raise
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                logger.warning("%s failed, retrying in %.1fs: %s", what, delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, config.RETRY_MAX_SECONDS)


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text
