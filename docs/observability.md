# Cloud Agent Observability

The operations dashboard is a standalone, read-only application. It shares
Postgres with every control-plane instance but does not import control code or
call a control instance, so its results do not depend on instance affinity.
The existing `/ui/` page remains the per-session event viewer served by
control.

```
controls (N) ──writes──▶ Postgres ◀──read-only── dashboard
       ▲                                              ▲
       └──────────── cursord daemons                  browser
```

## Correlation logs

Control and cursord write one JSON object per line. Every application record
has these stable fields:

- `service`, `timestamp`, `level`, `logger`, and `message`
- full `session_id`, also repeated as `trace_id`
- `epoch`, `action_id`, and `attempt`

Fields that do not apply are JSON `null`; UUIDs are never shortened. Control
binds context at HTTP and background-work boundaries. Cursord has session and
epoch process-wide and binds an action while it executes, checkpoints, pushes,
and reports. Known key, token, password, and secret environment values are
redacted from messages and exception strings.

LLM, clone, tool, git, and stale-epoch records add low-cardinality `event`
names and relevant fields such as `duration_ms`, `status_code`,
`error_category`, `tokens_in`, and `tokens_out`. These are diagnostic logs,
not yet a metrics exporter.

## Run the dashboard

The control and dashboard requirements currently overlap, but are kept
separate so the services can be packaged independently:

```bash
.venv/bin/pip install -r dashboard/requirements.txt
PYTHONPATH=. .venv/bin/uvicorn dashboard.app:app \
  --host 127.0.0.1 --port 8001
```

Open <http://127.0.0.1:8001/ui/>. The JSON snapshot is
`GET /api/overview?hours=24`; accepted windows are 1 through 168 hours.

Configuration:

| Variable | Default | Purpose |
| :- | :- | :- |
| `DASHBOARD_DATABASE_URL` | `DATABASE_URL` | libpq URL for the read-only user |
| `DASHBOARD_DB_POOL_MIN` | `1` | Minimum dashboard connections |
| `DASHBOARD_DB_POOL_MAX` | `4` | Maximum dashboard connections |
| `DASHBOARD_STATEMENT_TIMEOUT_MS` | `3000` | Per-query ceiling |
| `DASHBOARD_CACHE_SECONDS` | `10` | Snapshot cache lifetime |

The pool also sets `default_transaction_read_only=on`. Production should use a
database role that cannot write even if the application is misconfigured:

```sql
CREATE ROLE cloud_agent_dashboard LOGIN PASSWORD 'replace-me';
GRANT CONNECT ON DATABASE project1 TO cloud_agent_dashboard;
GRANT USAGE ON SCHEMA public TO cloud_agent_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cloud_agent_dashboard;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO cloud_agent_dashboard;
```

Do not put the password in source control. Set it only in the dashboard
deployment's `DASHBOARD_DATABASE_URL`.

## Metric definitions

All values use the selected dashboard window unless noted otherwise.

1. **Activity and outcomes.** Session starts come from `sessions.created_at`.
   A completed turn is a persisted `status=idle` event; a failed turn is a
   persisted `status=failed` event. Completion is completed divided by
   completed plus failed, grouped by outcome time.
2. **Active sessions by state.** Current `idle`, `thinking`, and `executing`
   session rows.
3. **Turn duration.** Not collected: no durable turn boundary exists.
4. **Oldest pending action.** Exact age of calls that are pending and have
   never been dispatched, plus all pending count. Requeued calls are excluded
   from age because there is no `pending_since`.
5. **Sandbox recovery.** Death events and sessions that subsequently reached
   `idle`. Unresolved sessions are displayed but excluded from the success
   denominator.
6. **LLM latency and errors.** Not collected in Postgres.
7. **Cost per completed turn.** Not collected in Postgres.
8. **Re-execution.** Calls with `attempts > 1` divided by calls with at least
   one dispatch.

Secondary panels report first-dispatch latency, control-observed
dispatch-to-result latency, sandbox row-to-ready latency, epoch high-water
mark, oldest thinking age, and executing sessions with no open tool call.
“Dispatch to result” includes daemon execution, checkpoint/push, and HTTP
reporting; it is not labeled as pure tool execution.

The dashboard never substitutes zero for unavailable telemetry. Every snapshot
includes its generation time, metric definitions, and cohort/sample counts.

## Deployment

The dashboard and control are separate failure and scaling domains. If one
browser origin is required, route them through a reverse proxy:

```text
/api/*  -> control
/ops/*  -> dashboard
```

The dashboard exposes only fixed aggregations. It has no arbitrary SQL
endpoint, uses a small pool, bounds the time window, caches snapshots, and
applies a statement timeout. A read replica can replace the primary in
`DASHBOARD_DATABASE_URL` without changing either application.

## Deferred exporter contract

The next telemetry increment should stay small and must not use session,
action, provider-call, or container IDs as metric labels.

- Control counters/histograms: advance claims won/lost, LLM attempts and
  latency by outcome, token totals, DB checkout wait, held long polls, poll
  query count, stale-epoch rejection reason.
- Daemon counters/histograms: actual tool duration by tool and exit-code
  class, clone/recovery duration, commit/push duration and failure class,
  stale 409s, heartbeat interval drift.
- Daemon gauges: container CPU, memory, and workspace disk.

Persisting `turn_id`/`turn_started` and `pending_since` should precede enabling
the two currently ambiguous duration/queue metrics.
