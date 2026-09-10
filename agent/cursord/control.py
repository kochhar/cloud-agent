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

    async def heartbeat(self) -> None:
        """Liveness. Raises StaleEpoch when the control plane has moved on."""
        await self._request(
            self._http.post(f"{self._base}/heartbeat", json={"epoch": config.EPOCH})
        )

    # -- work --------------------------------------------------------------

    async def next_action(self) -> Optional[Action]:
        """Long-poll for the pending tool call.

        None means the hold expired with nothing to do, which is the common
        case while the model is thinking or the session is awaiting the user.
        """
        body = await self._retrying(
            "next-action",
            lambda: self._http.get(
                f"{self._base}/next-action", params={"epoch": config.EPOCH}
            ),
        )

        # TODO(contract): the doc has no way to say "stop polling, we're done".
        # A null tool currently means both "nothing yet" and "session over", so
        # a finished session leaves its container polling forever. Proposing
        # the response also carry the session status.
        if body.get("session_status") in {"completed", "failed", "cancelled"}:
            raise SessionFinished(body["session_status"])

        tool = body.get("tool")
        if tool is None:
            return None
        return Action(
            action_id=tool["action_id"],
            name=tool["name"],
            args=tool.get("args") or {},
            attempt=tool.get("attempt", 1),
            repeated=tool.get("repeated", False),
        )

    async def report_result(
        self,
        action_id: str,
        *,
        result: str,
        exit_code: Optional[int],
        commit_sha: Optional[str],
    ) -> None:
        """Hand back a tool result. This is the call that advances the loop.

        Retried hard, because the work is already done and the commit is
        already pushed: giving up here is what turns a completed tool call
        into a duplicated one after recovery.
        """
        await self._retrying(
            "result",
            lambda: self._http.post(
                f"{self._base}/actions/{action_id}/result",
                json={
                    "epoch": config.EPOCH,
                    "result": result,
                    "exit_code": exit_code,
                    "commit_sha": commit_sha,
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
