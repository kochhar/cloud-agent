# project1

Python 3.9 project with a local virtualenv and a local PostgreSQL 16 server for FastAPI.

## Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Postgres

Postgres is not a system Homebrew service on this machine. It runs from binaries bundled in the venv (`pgserver`) with data in `.pgdata`.

```bash
./scripts/postgres.sh start    # listen on 127.0.0.1:5432
./scripts/postgres.sh status
./scripts/postgres.sh psql    # open a SQL shell
./scripts/postgres.sh stop
```

Connection URL for FastAPI:

```
postgresql://postgres@127.0.0.1:5432/project1
```

Auth is `trust` on localhost (no password). User `postgres`, database `project1`.

## FastAPI

```bash
source .venv/bin/activate
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000/health — it runs `SELECT version()` against Postgres.
