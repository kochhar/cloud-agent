"""HTTP surface for the control plane.

Every handler is a short step of a state machine that lives in Postgres. No
handler keeps anything in memory after it returns, so any instance can serve
any request for any session.

Handlers are plain `def`, not `async def`: the data layer is synchronous
psycopg, so FastAPI runs each one in the threadpool and a request that parks
on IO parks a thread rather than the event loop.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import anyio.to_thread
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import sandbox
import sessions

logging.basicConfig(level=logging.INFO)

# Each held long-poll occupies a thread for up to LONG_POLL_SECONDS, and every
# advance() parks one for the length of the model call. Two polls per active
# session means the stock 40 threads run out at ~20 sessions, and once they do
# even fast requests queue behind them.
THREADPOOL_TOKENS = int(os.environ.get("THREADPOOL_TOKENS", "200"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
    db.pool.wait(timeout=10)
    yield
    db.pool.close()


app = FastAPI(title="cloud-agent", lifespan=lifespan)



# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------
def _problem(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "detail": detail})


@app.exception_handler(sessions.SessionNotFound)
def _not_found(request: Request, exc: sessions.SessionNotFound) -> JSONResponse:
    return _problem(404, str(exc))


@app.exception_handler(sessions.UnknownAction)
def _unknown_action(request: Request, exc: sessions.UnknownAction) -> JSONResponse:
    return _problem(404, str(exc))


@app.exception_handler(sessions.StaleEpoch)
def _stale_epoch(request: Request, exc: sessions.StaleEpoch) -> JSONResponse:
    # 409 is the sandbox's cue to stop working and exit.
    return _problem(409, str(exc))


@app.exception_handler(sandbox.SpawnRefused)
def _spawn_refused(request: Request, exc: sandbox.SpawnRefused) -> JSONResponse:
    return _problem(409, str(exc))


def _not_built(what: str) -> HTTPException:
    return HTTPException(status_code=501, detail=f"{what} is not implemented yet")



# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    repo_url: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)


class CreateMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
    epoch: int
    container_id: Optional[str] = None


class ActionResultRequest(BaseModel):
    epoch: int
    result: Optional[str] = None
    exit_code: Optional[int] = None
    commit_sha: Optional[str] = None


class HeartbeatRequest(BaseModel):
    epoch: int



# ---------------------------------------------------------------------------
# operational
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    with db.pool.connection() as conn:
        row = conn.execute("SELECT version(), current_database()").fetchone()
    return {"ok": True, "database": row["current_database"], "version": row["version"]}


@app.post("/internal/reap")
def reap() -> Dict[str, Any]:
    """Respawn sandboxes with expired heartbeats. Driven by a timer."""
    raise _not_built("sandbox.reap")



# ---------------------------------------------------------------------------
# Client facing Session API
# Used by the client to create agent sessions and follow them.
# ---------------------------------------------------------------------------
@app.post("/sessions", status_code=201)
def create_session(body: CreateSessionRequest) -> Dict[str, Any]:
    # create_session spawns the sandbox and takes the first turn itself, so
    # there is nothing to kick off here.
    created = sessions.create_session(body.repo_url, body.prompt)
    return {
        "ok": True,
        "session_id": str(created["session_id"]),
        "branch": created["branch"],
    }


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    raise _not_built("sessions.get_session")


@app.post("/sessions/{session_id}/messages", status_code=202)
def create_message(session_id: str, body: CreateMessageRequest) -> Dict[str, Any]:
    raise _not_built("sessions.add_user_message")


@app.get("/sessions/{session_id}/events")
def get_events(
    session_id: str,
    after: int = Query(0, ge=0, description="Return events with seq greater than this."),
    limit: int = Query(500, ge=1, le=1000),
) -> Dict[str, Any]:
    """Long-poll the UI feed. An empty list means poll again with the same cursor."""
    events = sessions.get_events(session_id, after=after, limit=limit)
    return {
        "ok": True,
        "events": events,
        "next_after": events[-1]["seq"] if events else after,
    }


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str) -> Dict[str, Any]:
    raise _not_built("sessions.cancel_session")


@app.get("/sessions/{session_id}/diff")
def get_diff(session_id: str) -> Dict[str, Any]:
    raise _not_built("sessions.get_diff")



# ---------------------------------------------------------------------------
# Cursord facing API
# Used by the daemon inside the sandbox. Every call carries its epoch.
# ---------------------------------------------------------------------------
@app.post("/sandbox/{session_id}/register")
def register_sandbox(session_id: str, body: RegisterRequest) -> Dict[str, Any]:
    """Sandbox announces itself with its epoch."""
    registration = sandbox.register(session_id, body.epoch, body.container_id)
    return dict({"ok": True}, **registration)


@app.get("/sandbox/{session_id}/next-action")
def get_next_action(session_id: str, epoch: int = Query(..., ge=0)) -> Dict[str, Any]:
    """Long-poll for the pending tool call. A null tool means the hold expired."""
    action = sessions.claim_next_action(session_id, epoch)
    if action is None:
        return {"ok": True, "tool": None}
    return {
        "ok": True,
        "tool": {
            "action_id": str(action["id"]),
            "name": action["name"],
            "args": action["args"],
            "attempt": action["attempts"],
            "repeated": action["repeated"],
        },
    }


@app.post("/sandbox/{session_id}/actions/{action_id}/result")
def save_action_result(
    session_id: str, action_id: str, body: ActionResultRequest
) -> Dict[str, Any]:
    batch_complete = sessions.record_action_result(
        session_id,
        action_id,
        epoch=body.epoch,
        result=body.result,
        exit_code=body.exit_code,
        commit_sha=body.commit_sha,
    )
    # Parallel calls: only the last result of the batch advances the loop.
    if batch_complete:
        sessions.advance(session_id)
    return {"ok": True, "batch_complete": batch_complete}


@app.post("/sandbox/{session_id}/heartbeat")
def heartbeat(session_id: str, body: HeartbeatRequest) -> Dict[str, Any]:
    raise _not_built("sandbox.heartbeat")


# ---------------------------------------------------------------------------
# the browser client
#
# Served from the control plane rather than a static server of its own, so the
# page is same-origin with the API it calls. That is the whole reason: a client
# on another origin would need CORS configured here and a preflight on every
# request, to reach a service the browser is already talking to.
#
# Mounted last. A mount matches by prefix and would shadow any route declared
# after it.
# ---------------------------------------------------------------------------
CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"

if CLIENT_DIR.is_dir():

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/ui/")

    app.mount("/ui", StaticFiles(directory=CLIENT_DIR, html=True), name="ui")
else:
    logging.getLogger(__name__).warning(
        "no client/ directory at %s; the UI is not served", CLIENT_DIR
    )
