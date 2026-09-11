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
| `client/` | The browser client. Enqueues tasks and renders the event feed. |

`control/schema.sql` is the schema. It is applied by hand rather than on
boot:

```bash
./scripts/postgres.sh psql -f control/schema.sql
```

It is written to be safe to re-apply. The tables are `CREATE ... IF NOT
EXISTS`, which covers a new database and skips an existing one entirely, so
anything that has to change on a database already in use — a widened CHECK
constraint, a new column — is repeated in the `in-place changes` section at
the bottom of the file. Those must land with the code that writes the new
values, since a constraint tightened ahead of its code rejects what the
control plane is still sending.

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
the client is at http://127.0.0.1:8000/ui/, and the routes are browsable at
http://127.0.0.1:8000/docs.

To reach a real model, set `XAI_API_KEY` (optionally `LLM_MODEL`,
`LLM_BASE_URL`). Without it the first `advance` records a failure on the
session rather than raising, so the client sees it in the event feed.

## Client

The client is a page at http://127.0.0.1:8000/ui/. Three static files, no
build step and no dependencies: set the git workspace, enqueue a task, watch
the event feed. It holds no logic beyond rendering, as the architecture doc
asks of it.

`app.py` serves `client/` itself so the page is same-origin with the API it
calls. A client on its own origin would mean CORS configured on the control
plane and a preflight on every request, to reach a service the browser is
already talking to.

The git workspace URL is set once and kept in `localStorage`, because
`POST /sessions` wants a `repo_url` on every call. It is read on the control
plane's filesystem rather than the browser's, so a local bare repo has to be
given as an absolute path. The session list is browser-side too: there is no
route that lists sessions.

Reloading the page replays the feed of the most recent session from seq 0, so
a closed tab loses nothing. The feed stops on a terminal status.

Send, Diff and Cancel are wired to the routes in the doc and currently report
that the control plane has not built them, which is true: they are 501 in
`app.py`.

## Checks

```bash
.venv/bin/python scripts/smoke_loop.py
```

Drives the full loop against a scratch database with a scripted model: create,
register, dispatch, result, recovery from a dead sandbox, cancel.

```bash
.venv/bin/python scripts/check_tools.py
```

Compares the schemas in `control/tools.py` against the handlers in
`agent/cursord/tools.py`. The two halves ship in different images and cannot
import each other, so a renamed argument would otherwise stay invisible until
a live session called that tool and failed on every retry.

## The sandbox

`agent/` holds the container image and `cursord`. Outline and the open
contract questions it raised are in
[docs/sandbox-container.md](docs/sandbox-container.md).

```bash
docker build -t cloud-agent-sandbox:dev agent/
export SANDBOX_IMAGE=cloud-agent-sandbox:dev
```

Against a real git remote the container needs credentials. It runs
model-authored commands, so by default the host's ssh-agent socket is
forwarded rather than the key being mounted, which keeps the key out of a
filesystem the agent can read. `SANDBOX_SSH_MODE` is `agent`, `keys`, or
`none`; the trade-offs are in the doc above.

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts   # once
ssh-add ~/.ssh/id_ed25519                      # per login
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
