"""cursord must outlive a control plane that is failing, and must not outlive
one that has replaced it.

Both tests drive `heartbeat_loop` against a scripted control plane, so there
is no socket, no database and no container here. The distinction they pin
down is the whole contract of the heartbeat: a 409 is the control plane
telling this sandbox it has been superseded, and every other failure is the
control plane failing to say anything at all. Only the first ends the process.

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

# cursord is shipped in the container image and is not installed on the host,
# so the package root goes on the path the same way the spawner does it.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "agent")
)

from cursord import config  # noqa: E402
from cursord.__main__ import heartbeat_loop  # noqa: E402
from cursord.control import Control, StaleEpoch  # noqa: E402

CONTROL_URL = "http://control.test"


def _scripted(respond):
    """A Control whose control plane is `respond`, and the calls it received.

    The transport is swapped in after construction because Control builds its
    own client. If it ever grows an injectable transport, this function is the
    only thing that has to change.
    """
    control = Control()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return respond(request)

    control._http = httpx.AsyncClient(
        base_url=CONTROL_URL, transport=httpx.MockTransport(handler)
    )
    return control, calls


def _refused(request: httpx.Request) -> httpx.Response:
    """The control plane is not there at all, which is what a restart looks like."""
    raise httpx.ConnectError("connection refused", request=request)


async def _until(predicate, timeout: float = 2.0) -> None:
    """Wait on a condition rather than on the clock, so a loaded machine does
    not turn a passing test into a failing one."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for the heartbeat loop")


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # The loop reads the interval on every iteration, so setting the
        # attribute is enough. The environment variable behind it is only read
        # at import, which would make this depend on import order.
        self._interval = config.HEARTBEAT_INTERVAL
        config.HEARTBEAT_INTERVAL = 0.01
        config.SESSION_ID = "test-session"
        config.CONTROL_URL = CONTROL_URL
        config.EPOCH = 1
        # A warning per outage and a line per request is right in a container
        # and noise in a test report.
        logging.getLogger("cursord").setLevel(logging.CRITICAL)
        logging.getLogger("httpx").setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        config.HEARTBEAT_INTERVAL = self._interval

    async def test_keeps_beating_when_the_control_plane_fails(self) -> None:
        """A failed heartbeat must not take the process down.

        Regression: the heartbeat was the one call that did not go through the
        retrying path, so a 501 from an endpoint that is not built yet raised
        out of its task, and `main` cancelled the work loop and exited. A
        sandbox midway through a tool call died for a reason that had nothing
        to do with the tool call.
        """
        failures = {
            "501 from an endpoint that is not built": lambda request: httpx.Response(
                501, json={"detail": "sandbox.heartbeat is not implemented yet"}
            ),
            "control plane restarting": _refused,
            "gateway between us is down": lambda request: httpx.Response(502),
        }

        for description, respond in failures.items():
            with self.subTest(failure=description):
                control, calls = _scripted(respond)
                task = asyncio.ensure_future(heartbeat_loop(control))
                try:
                    # Several beats, so this is a loop that survived rather
                    # than one that had not failed yet. Waiting on the task as
                    # well means a loop that dies fails here immediately, and
                    # says what killed it instead of timing out.
                    await _until(lambda: len(calls) >= 3 or task.done())
                    if task.done():
                        self.fail(
                            "the heartbeat loop exited on a failed beat: {!r}".format(
                                task.exception()
                            )
                        )
                finally:
                    task.cancel()
                    await control.aclose()

    async def test_exits_when_the_control_plane_says_the_epoch_is_stale(self) -> None:
        """A 409 is the one failure that ends this container.

        A newer sandbox owns the session, so anything still in flight here is
        already worthless: the branch will be reset past whatever this epoch
        pushed and no result it reports will be accepted.
        """
        control, calls = _scripted(
            lambda request: httpx.Response(409, json={"detail": "epoch 1 < epoch 2"})
        )
        try:
            with self.assertRaises(StaleEpoch):
                await asyncio.wait_for(heartbeat_loop(control), timeout=2)
            self.assertEqual(
                len(calls), 1, "a stale epoch must not be retried, it is final"
            )
        finally:
            await control.aclose()


if __name__ == "__main__":
    unittest.main()
