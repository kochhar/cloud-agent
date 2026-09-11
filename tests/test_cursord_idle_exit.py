"""An idle session eventually stops being worth a container.

The work loop leaves when the control plane has reported 'idle' continuously
for longer than the threshold. The two things worth pinning down are that it
does leave, and that it does not leave early — a session that is merely
between tool calls looks identical from here except for the status, and
mistaking one for the other abandons work that is about to arrive.

Run with:

    .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import unittest

import httpx

from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "agent")
)

from cursord import config  # noqa: E402
from cursord.__main__ import work_loop  # noqa: E402
from cursord.control import Control, StaleEpoch  # noqa: E402

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


def _poll(status, tool=None):
    return httpx.Response(200, json={"ok": True, "session_status": status, "tool": tool})


class _Finished:
    """What tools.execute hands back, without a filesystem underneath it."""

    output = "ok"
    exit_code = 0


class _Checkpoint:
    sha = None
    preview = None
    stat = None


async def _no_execution(name, args):
    return _Finished()


async def _no_checkpoint(message, base):
    return _Checkpoint()


def _without_a_workspace():
    """Run the loop's tool branch without a clone to run it in.

    These tests are about when the loop decides to leave, not about what a
    tool does. Left unpatched, a dispatched call shells out to git in
    /workspace/repo, which does not exist on the host running the tests.
    """
    return mock.patch.multiple(
        "cursord.tools", execute=_no_execution
    ), mock.patch.multiple("cursord.workspace", checkpoint=_no_checkpoint)


class IdleExitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._idle_exit = config.IDLE_EXIT_SECONDS
        config.SESSION_ID = "test-session"
        config.CONTROL_URL = CONTROL_URL
        config.EPOCH = 1
        logging.getLogger("cursord").setLevel(logging.CRITICAL)
        logging.getLogger("httpx").setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        config.IDLE_EXIT_SECONDS = self._idle_exit

    async def test_leaves_once_the_session_has_been_idle_for_the_threshold(self):
        config.IDLE_EXIT_SECONDS = 0.05
        control, calls = _scripted(lambda request: _poll("idle"))
        try:
            reason = await asyncio.wait_for(work_loop(control, base="abc123"), timeout=2)
            self.assertEqual(reason, "idle")
            # The first idle answer starts the clock rather than ending the
            # loop, so leaving takes at least two of them.
            self.assertGreaterEqual(len(calls), 2)
        finally:
            await control.aclose()

    async def test_stays_while_the_session_is_working(self):
        """'executing' with no tool is the ordinary mid-turn poll, not an exit.

        The threshold is set below the poll interval, so a loop that counted
        'executing' as idle would leave on the second answer. Driven to a
        409 instead of being cancelled, because the assertion is about which
        way the loop ended: 'idle' returned here would be the bug.
        """
        config.IDLE_EXIT_SECONDS = 0.001
        state = {"polls": 0}

        def respond(request: httpx.Request) -> httpx.Response:
            state["polls"] += 1
            if state["polls"] > 5:
                # Something has to stop a loop that is behaving correctly.
                return httpx.Response(409, json={"detail": "epoch 1 < epoch 2"})
            return _poll("executing")

        control, _ = _scripted(respond)
        try:
            with self.assertRaises(StaleEpoch):
                await asyncio.wait_for(work_loop(control, base="abc123"), timeout=5)
            self.assertEqual(state["polls"], 6)
        finally:
            await control.aclose()

    async def test_a_session_that_resumes_resets_the_clock(self):
        """Work arriving means the wait starts over, not continues."""
        config.IDLE_EXIT_SECONDS = 0.2
        state = {"polls": 0}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/result"):
                return httpx.Response(200, json={"ok": True})
            state["polls"] += 1
            # Idle for a while, then a tool call lands, then idle again. If
            # the clock carried across the work, the loop would leave almost
            # immediately after; it should serve the full threshold again.
            if state["polls"] == 3:
                return _poll("executing", {
                    "action_id": "a1", "name": "list_files",
                    "args": {"path": "."}, "attempt": 1, "repeated": False,
                })
            return _poll("idle")

        control, _ = _scripted(respond)
        no_tools, no_git = _without_a_workspace()
        started = time.monotonic()
        try:
            with no_tools, no_git:
                reason = await asyncio.wait_for(
                    work_loop(control, base="abc123"), timeout=5
                )
            elapsed = time.monotonic() - started
            self.assertEqual(reason, "idle")
            self.assertGreaterEqual(
                elapsed, config.IDLE_EXIT_SECONDS,
                "left without serving a full idle threshold after the work",
            )
        finally:
            await control.aclose()

    async def _until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out waiting for the work loop")


if __name__ == "__main__":
    unittest.main()
