from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import config  # noqa: E402
from dashboard.app import create_app  # noqa: E402


class DashboardIntegrationTests(unittest.TestCase):
    """The dashboard reads seeded Postgres without any control process."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = "dashboard_test_{}".format(uuid.uuid4().hex)
        cls.admin = psycopg.connect(config.DATABASE_URL, autocommit=True)
        cls.admin.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema))
        )
        cls.admin.execute(
            sql.SQL("SET search_path TO {}").format(sql.Identifier(cls.schema))
        )
        cls.admin.execute((ROOT / "control" / "schema.sql").read_text())
        cls._seed()

        cls.pool = ConnectionPool(
            config.DATABASE_URL,
            min_size=1,
            max_size=2,
            kwargs={
                "row_factory": dict_row,
                "options": (
                    f"-c search_path={cls.schema} "
                    "-c default_transaction_read_only=on "
                    "-c statement_timeout=3000"
                ),
            },
            open=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.admin.execute("SET search_path TO public")
        cls.admin.execute(
            sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
        )
        cls.admin.close()

    @classmethod
    def _seed(cls) -> None:
        sessions = []
        for status, age in (("idle", "2 hours"), ("failed", "1 hour"), ("executing", "30 minutes")):
            row = cls.admin.execute(
                """
                INSERT INTO sessions (repo_url, branch, status, current_epoch, created_at)
                VALUES ('git@example/repo', %s, %s, 2, now() - %s::interval)
                RETURNING id
                """,
                ("agent/" + status, status, age),
            ).fetchone()
            sessions.append(row[0])
        recovered, failed, pending = sessions

        cls.admin.execute(
            """
            INSERT INTO events (session_id, seq, type, payload, created_at) VALUES
              (%s, 1, 'sandbox_ready', '{"epoch": 1}', now() - interval '50 minutes'),
              (%s, 2, 'sandbox_died', '{"epoch": 1, "reason": "heartbeat_expired"}',
                    now() - interval '40 minutes'),
              (%s, 3, 'status', '{"status": "idle"}', now() - interval '30 minutes'),
              (%s, 1, 'status', '{"status": "failed"}', now() - interval '20 minutes')
            """,
            (recovered, recovered, recovered, failed),
        )
        cls.admin.execute(
            """
            INSERT INTO sandboxes
                (session_id, epoch, status, created_at, last_heartbeat_at)
            VALUES (%s, 1, 'replaced', now() - interval '51 minutes',
                    now() - interval '50 minutes')
            """,
            (recovered,),
        )

        message_ids = []
        for sequence, session_id in enumerate((recovered, pending), start=1):
            row = cls.admin.execute(
                """
                INSERT INTO messages (session_id, seq, role, content)
                VALUES (%s, %s, 'assistant', 'tool call') RETURNING id
                """,
                (session_id, sequence),
            ).fetchone()
            message_ids.append(row[0])

        cls.admin.execute(
            """
            INSERT INTO tool_calls
                (session_id, message_id, provider_call_id, name, args, status,
                 epoch, attempts, repeated, dispatched_at, completed_at, created_at)
            VALUES
                (%s, %s, 'repeated-call', 'read_file', '{}', 'done',
                 2, 2, true, now() - interval '25 minutes',
                 now() - interval '24 minutes', now() - interval '27 minutes'),
                (%s, %s, 'pending-call', 'read_file', '{}', 'pending',
                 NULL, 0, false, NULL, NULL, now() - interval '15 minutes')
            """,
            (recovered, message_ids[0], pending, message_ids[1]),
        )

        retried_call = uuid.uuid4()
        failed_call = uuid.uuid4()
        cls.admin.execute(
            """
            INSERT INTO llm_attempts
                (session_id, call_id, attempt, is_final, provider, model, outcome,
                 http_status, input_tokens, output_tokens, context_chars,
                 message_count, tool_call_count, estimated_cost_usd,
                 duration_ms, completed_at, started_at)
            VALUES
                (%s, %s, 1, false, 'xai', 'test-model', 'rate_limit',
                 429, NULL, NULL, 1000, 4, NULL, NULL,
                 100, now() - interval '29 minutes', now() - interval '29 minutes'),
                (%s, %s, 2, true, 'xai', 'test-model', 'success',
                 200, 1000, 100, 1000, 4, 1, 0.03,
                 1000, now() - interval '28 minutes', now() - interval '28 minutes'),
                (%s, %s, 1, true, 'xai', 'test-model', 'timeout',
                 NULL, NULL, NULL, 500, 2, NULL, NULL,
                 3000, now() - interval '19 minutes', now() - interval '19 minutes')
            """,
            (
                recovered,
                retried_call,
                recovered,
                retried_call,
                failed,
                failed_call,
            ),
        )

    def test_overview_uses_exact_postgres_cohorts(self) -> None:
        app = create_app(self.pool)
        with TestClient(app) as client:
            response = client.get("/api/overview?hours=24")
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()

            self.assertEqual(data["tiles"]["activity"]["turns_completed"], 1)
            self.assertEqual(data["tiles"]["activity"]["turns_failed"], 1)
            self.assertEqual(data["tiles"]["activity"]["completion_rate"], 0.5)
            self.assertEqual(data["tiles"]["queue"]["never_dispatched_count"], 1)
            self.assertGreater(
                data["tiles"]["queue"]["oldest_never_dispatched_seconds"], 800
            )
            self.assertEqual(data["tiles"]["recovery"]["deaths"], 1)
            self.assertEqual(data["tiles"]["recovery"]["success_rate"], 1)
            self.assertEqual(data["tiles"]["reexecution"]["reexecuted_calls"], 1)
            self.assertFalse(data["tiles"]["turn_duration"]["available"])
            self.assertTrue(data["tiles"]["llm"]["available"])
            self.assertAlmostEqual(
                data["tiles"]["llm"]["attempt_error_rate"], 2 / 3
            )
            self.assertEqual(data["tiles"]["llm"]["final_call_error_rate"], 0.5)
            self.assertEqual(data["tiles"]["llm"]["p95_ms"], 1000)
            self.assertTrue(data["tiles"]["cost"]["available"])
            self.assertEqual(
                data["tiles"]["cost"]["cost_per_completed_turn_usd"], 0.03
            )
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.get("/api/overview?hours=169").status_code, 422)


if __name__ == "__main__":
    unittest.main()
