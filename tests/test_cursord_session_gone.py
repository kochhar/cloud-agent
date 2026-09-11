"""A sandbox whose session no longer exists must stop, not keep asking.

404 is the third answer that ends a container, alongside 409 and a terminal
session. It is different from the others in how it used to fail: a missing
session went down the retry path, which has no attempt ceiling on purpose,
so the daemon asked forever. Deleting a session left a process polling for
it until someone found it and killed it by hand.

The pair of register tests is the point of this file. A control plane that
cannot be reached and a control plane that answers 404 arrive at the same
call and mean opposite things: the first has said nothing, and giving up on
it would kill a container over a restart; the second has said the session
is not there, and retrying it is the bug above.

Run with:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import unittest

import httpx

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "agent")
)

from cursord import config  # noqa: E402
from cursord.__main__ import heartbeat_loop  # noqa: E402
from cursord.control import Control, SessionGone  # noqa: E402

CONTROL_URL = "http://control.test"


def _scripted(respond):
    """A Control talking to a scripted control plane, and the calls it made."""
    control = Control()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return respond(request)

    control._http = httpx.AsyncClient(
        base_url=CONTROL_URL, transport=httpx.MockTransport(handler)
    )
    return control, calls


def _no_such_session(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": "no session test-session"})


class SessionGoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._interval = config.HEARTBEAT_INTERVAL
        self._backoff = config.RETRY_BASE_SECONDS
        config.HEARTBEAT_INTERVAL = 0.01
        # Read on every retry, so shrinking it here keeps the backoff real
        # while costing milliseconds rather than seconds.
        config.RETRY_BASE_SECONDS = 0.01
        config.SESSION_ID = "test-session"
        config.CONTROL_URL = CONTROL_URL
        config.EPOCH = 1
        logging.getLogger("cursord").setLevel(logging.CRITICAL)
        logging.getLogger("httpx").setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        config.HEARTBEAT_INTERVAL = self._interval
        config.RETRY_BASE_SECONDS = self._backoff

    async def test_register_waits_out_a_control_plane_that_is_not_up(self) -> None:
        """The other half: an unreachable control plane has said nothing.

        The container regularly wins the race against the instance that
        spawned it, so refusing a connection is a timing artifact. Giving up
        here would kill a sandbox over a control plane restart.
        """
        attempts = {"count": 0}

        def respond(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "repo_url": "git@github.com:acme/widgets.git",
                    "branch": "agent/abc12345",
                    "base_sha": "a" * 40,
                    "resume_sha": None,
                },
            )

        control, calls = _scripted(respond)
        try:
            registration = await asyncio.wait_for(control.register(), timeout=5)
            self.assertEqual(registration.branch, "agent/abc12345")
            self.assertEqual(len(calls), 3, "it should have waited, not given up")
        finally:
            await control.aclose()

    async def test_register_gives_up_on_a_session_that_is_not_there(self) -> None:
        """The reported case: register 404s and the daemon asks again forever.

        One call, because spawn commits the session row before it starts this
        process. A 404 here cannot be the container winning a race, which is
        the only thing retrying register was ever meant to survive.
        """
        control, calls = _scripted(_no_such_session)
        try:
            with self.assertRaises(SessionGone):
                await asyncio.wait_for(control.register(), timeout=2)
            self.assertEqual(len(calls), 1, "a missing session must not be retried")
        finally:
            await control.aclose()

    async def test_the_heartbeat_stops_too(self) -> None:
        """Not just register: the same answer to any call ends the container.

        A session can be deleted after a sandbox has registered, which leaves
        the beat as the thing that finds out.
        """
        control, calls = _scripted(_no_such_session)
        try:
            with self.assertRaises(SessionGone):
                await asyncio.wait_for(heartbeat_loop(control), timeout=2)
            self.assertEqual(len(calls), 1)
        finally:
            await control.aclose()

    async def test_the_long_poll_stops_too(self) -> None:
        """The poll is where an orphan spends its life, so it has to end here."""
        control, calls = _scripted(_no_such_session)
        try:
            with self.assertRaises(SessionGone):
                await asyncio.wait_for(control.next_action(), timeout=2)
            self.assertEqual(len(calls), 1)
        finally:
            await control.aclose()


if __name__ == "__main__":
    unittest.main()
