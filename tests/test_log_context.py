from __future__ import annotations

import io
import json
import logging
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "control"))

import log_context  # noqa: E402


class LogContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(log_context.JsonFormatter())
        self.logger = logging.getLogger("test.correlation")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)

    def tearDown(self) -> None:
        self.logger.handlers = []

    def test_context_is_structured_and_does_not_leak(self) -> None:
        with log_context.bind(
            session_id="session-full-uuid", epoch=3, action_id="action-1", attempt=2
        ):
            self.logger.info("ran action")
        self.logger.info("global")

        first, second = [
            json.loads(line) for line in self.stream.getvalue().splitlines()
        ]
        self.assertEqual(first["trace_id"], "session-full-uuid")
        self.assertEqual(first["session_id"], "session-full-uuid")
        self.assertEqual(first["epoch"], 3)
        self.assertEqual(first["action_id"], "action-1")
        self.assertEqual(first["attempt"], 2)
        self.assertIsNone(second["session_id"])
        self.assertIsNone(second["action_id"])

    def test_configured_secrets_are_redacted(self) -> None:
        secret = "test-secret-value"
        os.environ["TEST_API_KEY"] = secret
        try:
            log_context.configure("test")
            self.logger.info("provider said %s", secret)
            payload = json.loads(self.stream.getvalue())
            self.assertEqual(payload["message"], "provider said [REDACTED]")
        finally:
            os.environ.pop("TEST_API_KEY", None)
            log_context.configure("control")


if __name__ == "__main__":
    unittest.main()
