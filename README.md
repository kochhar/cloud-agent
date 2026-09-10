# project1 — local cloud agent

Control plane for a local version of a cloud coding agent. Design lives in
[docs/local-cloud-agent-architecture.md](docs/local-cloud-agent-architecture.md).

## Layout

| Path | What it holds |
| :- | :- |
| `app.py` | HTTP surface. Every handler is one step of a state machine. |
| `control/sessions.py` | Session state and the agent loop: `advance`, tool dispatch, results, events. |
| `control/sandbox.py` | Epochs, heartbeats, recovery, the reaper. |
| `control/llm.py` | The model call, behind an injectable seam. |
| `control/tools.py` | Tool schemas handed to the model. Deliberately no git. |
| `control/models.py` | Row objects, one per table. |
| `control/config.py` | Timeouts and ceilings, overridable from the environment. |
| `db/` | Pool, transaction helpers, and the executable schema. |
| `agent/` | The container image and `cursord`, the daemon inside it. |

`db/schema.sql` is what actually runs and is applied on startup.
`docs/schema.sql` is the design document, and additionally carries the three
illustrative queries.

## Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Postgres

Postgres is not a system service here. It runs from binaries bundled in the
venv (`pgserver`) with data in `.pgdata`.

```bash
./scripts/postgres.sh start    # 127.0.0.1:5432
./scripts/postgres.sh status
./scripts/postgres.sh psql
./scripts/postgres.sh stop
```

Connection URL, also in `.env`:

```
postgresql://postgres@127.0.0.1:5432/project1
```

## Run

```bash
source .venv/bin/activate
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Tables are created on startup if missing. `GET /healthz` checks the database,
and the routes are browsable at http://127.0.0.1:8000/docs.

To reach a real model, set `XAI_API_KEY` (optionally `LLM_MODEL`,
`LLM_BASE_URL`). Without it the first `advance` records a failure on the
session rather than raising, so the client sees it in the event feed.

## Checks

```bash
.venv/bin/python scripts/smoke_loop.py
```

Drives the full loop against a scratch database with a scripted model: create,
register, dispatch, result, recovery from a dead sandbox, cancel.

## The sandbox

`agent/` holds the container image and `cursord`. Outline and the open
contract questions it raised are in
[docs/sandbox-container.md](docs/sandbox-container.md).

```bash
docker build -t cloud-agent-sandbox:dev agent/
export SANDBOX_IMAGE=cloud-agent-sandbox:dev
```

## Not built yet

- `cursord` is a sketch: it has not been run against a live control plane, and
  the clone/checkpoint path has not been exercised against a real bare repo.
- Container spawning. `control/sandbox.py` records the sandbox row and opens
  the epoch, but the default spawner starts nothing; install one with
  `sandbox.use_spawner()`.
- `sandbox.register`, `heartbeat`, `reap` and `teardown`, all of which `app.py`
  already calls.
- A timer calling `POST /internal/reap`.
