from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "control"))

import config  # noqa: E402
import llm  # noqa: E402
import telemetry  # noqa: E402


class LlmTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = (
            config.GROK_API_KEY,
            config.GROK_MAX_ATTEMPTS,
            config.GROK_INPUT_COST_PER_MILLION,
            config.GROK_OUTPUT_COST_PER_MILLION,
            llm._http,
        )
        config.GROK_API_KEY = "test-key"
        config.GROK_MAX_ATTEMPTS = 2

    def tearDown(self) -> None:
        if llm._http is not None and llm._http is not self.saved[4]:
            llm._http.close()
        (
            config.GROK_API_KEY,
            config.GROK_MAX_ATTEMPTS,
            config.GROK_INPUT_COST_PER_MILLION,
            config.GROK_OUTPUT_COST_PER_MILLION,
            llm._http,
        ) = self.saved

    def test_retry_attempts_share_call_id_and_mark_only_terminal_row(self) -> None:
        responses = iter(
            (
                httpx.Response(429, json={"error": "slow down"}),
                httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "done"},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 120,
                            "completion_tokens": 8,
                        },
                    },
                ),
            )
        )

        def respond(request: httpx.Request) -> httpx.Response:
            response = next(responses)
            response.request = request
            return response

        llm._http = httpx.Client(
            base_url="https://provider.test",
            transport=httpx.MockTransport(respond),
        )

        with (
            mock.patch.object(
                llm.telemetry,
                "start_llm_attempt",
                side_effect=("attempt-1", "attempt-2"),
            ) as start,
            mock.patch.object(llm.telemetry, "finish_llm_attempt") as finish,
            mock.patch.object(llm.time, "sleep"),
        ):
            reply = llm._grok([{"role": "user", "content": "hello"}], "session-id")

        self.assertEqual(reply.text, "done")
        self.assertEqual(start.call_count, 2)
        first_call_id = start.call_args_list[0].kwargs["call_id"]
        self.assertEqual(start.call_args_list[1].kwargs["call_id"], first_call_id)
        self.assertFalse(finish.call_args_list[0].kwargs["is_final"])
        self.assertEqual(finish.call_args_list[0].kwargs["outcome"], "rate_limit")
        self.assertTrue(finish.call_args_list[1].kwargs["is_final"])
        self.assertEqual(finish.call_args_list[1].kwargs["outcome"], "success")
        self.assertEqual(finish.call_args_list[1].kwargs["input_tokens"], 120)

    def test_cost_requires_a_complete_price_snapshot(self) -> None:
        config.GROK_INPUT_COST_PER_MILLION = 2.0
        config.GROK_OUTPUT_COST_PER_MILLION = 10.0
        self.assertEqual(
            telemetry._estimated_cost(1_000_000, 100_000), Decimal("3.0")
        )
        config.GROK_OUTPUT_COST_PER_MILLION = None
        self.assertIsNone(telemetry._estimated_cost(1_000_000, 100_000))


if __name__ == "__main__":
    unittest.main()
