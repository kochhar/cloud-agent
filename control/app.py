"""HTTP surface for the control plane.

Each handler is one step of a state machine that lives in Postgres, and keeps
nothing in memory after it returns, so any instance can serve any request.

Handlers are plain `def`: the data layer is synchronous psycopg, so FastAPI
runs them in the threadpool and a request that parks on IO parks a thread
rather than the event loop.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import anyio.to_thread
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import db
import log_context
import nudges
import sandbox
import sessions

log_context.configure()

# Every held long-poll and every advance() parks a thread. At two polls per
# active session the stock 40 would run out around 20 sessions, after which
# even fast requests queue.
THREADPOOL_TOKENS = int(os.environ.get("THREADPOOL_TOKENS", "200"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_TOKENS
    db.pool.wait(timeout=10)

    # Sweep before serving: a restart is when sessions are most likely to be
    # stranded. In a thread because a pass can spawn containers, and blocking
    # the event loop would delay the health check.
    stop = threading.Event()
    await anyio.to_thread.run_sync(nudges.run_once)
    nudger = threading.Thread(
        target=nudges.run_forever, args=(stop,), name="nudger", daemon=True
    )
    nudger.start()

    yield

    # One interval to notice. Daemon, so a pass in flight cannot hold us open.
    stop.set()
    nudger.join(timeout=config.NUDGE_INTERVAL_SECONDS)
    db.pool.close()


app = FastAPI(title="cloud-agent", lifespan=lifespan)

_SESSION_PATH = re.compile(r"/(?:sessions|sandbox)/([^/]+)")
_ACTION_PATH = re.compile(r"/actions/([^/]+)")


@app.middleware("http")
async def correlate_request(request: Request, call_next):
    """Put path identifiers on access and application logs for this request."""
    session = _SESSION_PATH.search(request.url.path)
    action = _ACTION_PATH.search(request.url.path)
    epoch = request.query_params.get("epoch")
    with log_context.bind(
        session_id=session.group(1) if session else None,
        action_id=action.group(1) if action else None,
        epoch=int(epoch) if epoch and epoch.isdigit() else None,
    ):
        return await call_next(request)



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


@app.exception_handler(sessions.ActionNotDispatched)
def _not_dispatched(request: Request, exc: sessions.ActionNotDispatched) -> JSONResponse:
    # The call exists, so this is a conflict with its state, not a 404.
    return _problem(409, str(exc))


@app.exception_handler(sessions.StaleEpoch)
def _stale_epoch(request: Request, exc: sessions.StaleEpoch) -> JSONResponse:
    # 409 is the sandbox's cue to stop working and exit.
    return _problem(409, str(exc))


@app.exception_handler(sessions.SessionFinished)
def _session_finished(request: Request, exc: sessions.SessionFinished) -> JSONResponse:
    return _problem(409, str(exc))


@app.exception_handler(sandbox.SpawnRefused)
def _spawn_refused(request: Request, exc: sandbox.SpawnRefused) -> JSONResponse:
    return _problem(409, str(exc))


@app.exception_handler(sandbox.SandboxNotRegistered)
def _sandbox_unknown(
    request: Request, exc: sandbox.SandboxNotRegistered
) -> JSONResponse:
    return _problem(404, str(exc))


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
    # Git lives in the data plane, so the diff summary is whatever the sandbox
    # sends: a preview rather than the patch, a stat rather than contents.
    base_sha: Optional[str] = None
    diff_preview: Optional[str] = None
    diff_stat: Optional[dict] = None


class HeartbeatRequest(BaseModel):
    epoch: int
    # Set on the way out, so a deliberate exit is distinguishable from a
    # container that simply stopped answering.
    exiting: bool = False
    reason: Optional[str] = None



# ---------------------------------------------------------------------------
# operational
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, **db.health()}


@app.post("/internal/reap")
def reap() -> Dict[str, Any]:
    """Run one nudger pass now and report what it moved.

    The same pass the daemon thread runs, exposed so recovery can be triggered
    and inspected by hand.
    """
    return {"ok": True, **nudges.run_once()}



# ---------------------------------------------------------------------------
# Client facing Session API
# Used by the client to create agent sessions and follow them.
# ---------------------------------------------------------------------------
@app.post("/sessions", status_code=201)
def create_session(body: CreateSessionRequest) -> Dict[str, Any]:
    return {"ok": True, **sessions.create_session(body.repo_url, body.prompt)}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    raise _not_built("sessions.get_session")


@app.post("/sessions/{session_id}/messages", status_code=202)
def create_message(session_id: str, body: CreateMessageRequest) -> Dict[str, Any]:
    """Add a user message. Advances the loop if nothing else is running."""
    return {"ok": True, **sessions.add_user_message(session_id, body.content)}


@app.get("/sessions/{session_id}/events")
def get_events(
    session_id: str,
    after: int = Query(0, ge=0, description="Return events with seq greater than this."),
    limit: int = Query(500, ge=1, le=1000),
) -> Dict[str, Any]:
    """Long-poll the UI feed. An empty list means poll again with the same cursor."""
    return {"ok": True, **sessions.get_events(session_id, after=after, limit=limit)}


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str) -> Dict[str, Any]:
    raise _not_built("sessions.cancel_session")


@app.get("/sessions/{session_id}/diff")
def get_diff(session_id: str) -> Dict[str, Any]:
    """The branch against its base, as the control plane accepted it."""
    return {"ok": True, **sessions.get_diff(session_id)}



# ---------------------------------------------------------------------------
# Cursord facing API
# Used by the daemon inside the sandbox. Every call carries its epoch.
# ---------------------------------------------------------------------------
@app.post("/sandbox/{session_id}/register")
def register_sandbox(session_id: str, body: RegisterRequest) -> Dict[str, Any]:
    """Sandbox announces itself with its epoch."""
    return {"ok": True, **sandbox.register(session_id, body.epoch, body.container_id)}


@app.get("/sandbox/{session_id}/next-action")
def get_next_action(session_id: str, epoch: int = Query(..., ge=0)) -> Dict[str, Any]:
    """Long-poll for the pending tool call.

    A null tool means the hold expired with nothing pending, unless
    session_status is terminal, in which case the sandbox should exit.
    """
    return {"ok": True, **sessions.claim_next_action(session_id, epoch)}


@app.post("/sandbox/{session_id}/actions/{action_id}/result")
def save_action_result(
    session_id: str,
    action_id: str,
    body: ActionResultRequest,
    background: BackgroundTasks,
) -> Dict[str, Any]:
    return {
        "ok": True,
        **sessions.record_action_result(
            session_id,
            action_id,
            epoch=body.epoch,
            result=body.result,
            exit_code=body.exit_code,
            commit_sha=body.commit_sha,
            base_sha=body.base_sha,
            diff_preview=body.diff_preview,
            diff_stat=body.diff_stat,
            # Anything sessions wants to run once this response is sent.
            schedule=background.add_task,
        ),
    }


@app.post("/sandbox/{session_id}/heartbeat")
def heartbeat(session_id: str, body: HeartbeatRequest) -> Dict[str, Any]:
    return {
        "ok": True,
        **sandbox.heartbeat(
            session_id, body.epoch, exiting=body.exiting, reason=body.reason
        ),
    }


# ---------------------------------------------------------------------------
# the browser client
#
# Served here so the page is same-origin with the API, avoiding CORS and a
# preflight on every request. Mounted last: a mount matches by prefix and
# would shadow any route declared after it.
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
