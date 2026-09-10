"""HTTP surface for the control plane.

Every handler is a short step of a state machine that lives in Postgres. No
handler keeps anything in memory after it returns, so any instance can serve
any request for any session.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
import sandbox
import sessions

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open_pool()
    await db.apply_schema()
    yield
    await db.close_pool()


app = FastAPI(title="cloud-agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------


def _problem(status: int, detail: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict({"ok": False, "detail": detail}, **extra))


@app.exception_handler(sessions.SessionNotFound)
async def _not_found(request: Request, exc: sessions.SessionNotFound) -> JSONResponse:
    return _problem(404, str(exc))


@app.exception_handler(sessions.UnknownAction)
async def _unknown_action(request: Request, exc: sessions.UnknownAction) -> JSONResponse:
    return _problem(404, str(exc))


@app.exception_handler(sessions.StaleEpoch)
async def _stale_epoch(request: Request, exc: sessions.StaleEpoch) -> JSONResponse:
    # 409 is the sandbox's cue to stop working and exit.
    return _problem(409, str(exc), epoch=exc.given, current_epoch=exc.current)


@app.exception_handler(sessions.InvalidSessionState)
async def _invalid_state(request: Request, exc: sessions.InvalidSessionState) -> JSONResponse:
    return _problem(409, str(exc))


@app.exception_handler(ValueError)
async def _bad_request(request: Request, exc: ValueError) -> JSONResponse:
    return _problem(400, str(exc))


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    repo_url: str = Field(..., min_length=1)
    prompt: Optional[str] = None


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
async def healthz() -> Dict[str, Any]:
    try:
        async with db.connection() as conn:
            cur = await conn.execute("SELECT version(), current_database()")
            row = await cur.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "database": row["current_database"], "version": row["version"]}


@app.post("/internal/reap")
async def reap() -> Dict[str, Any]:
    """Respawn sandboxes with expired heartbeats. Driven by a timer, not a worker."""
    return dict({"ok": True}, **await sandbox.reap())


# ---------------------------------------------------------------------------
# Client facing Session API
# Used by the client to create agent sessions and follow them.
# ---------------------------------------------------------------------------


@app.post("/sessions", status_code=201)
async def create_session(body: CreateSessionRequest) -> Dict[str, Any]:
    session = await sessions.create_session(body.repo_url, body.prompt)
    # The session row exists before any container does; the sandbox catches up
    # while the first model call runs.
    await sandbox.spawn(session.id)
    if body.prompt:
        await sessions.advance(session.id)
    return {"ok": True, "session_id": str(session.id), "branch": session.branch}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> Dict[str, Any]:
    return {"ok": True, "session": await sessions.get_session(session_id)}


@app.post("/sessions/{session_id}/messages", status_code=202)
async def create_message(session_id: str, body: CreateMessageRequest) -> Dict[str, Any]:
    message = await sessions.add_user_message(session_id, body.content)
    await sessions.advance(session_id)
    return {"ok": True, "message_id": str(message.id), "seq": message.seq}


@app.get("/sessions/{session_id}/events")
async def get_events(
    session_id: str,
    after: int = Query(0, ge=0, description="Return events with seq greater than this."),
    limit: int = Query(500, ge=1, le=1000),
) -> Dict[str, Any]:
    """Long-poll the UI feed. An empty list means poll again with the same cursor."""
    events = await sessions.get_events(session_id, after, limit=limit)
    return {
        "ok": True,
        "events": events,
        "next_after": events[-1].seq if events else after,
    }


@app.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str) -> Dict[str, Any]:
    session = await sessions.cancel_session(session_id)
    await sandbox.teardown(session_id)
    return {"ok": True, "status": session.status}


@app.get("/sessions/{session_id}/diff")
async def get_diff(session_id: str) -> Dict[str, Any]:
    return {"ok": True, "diff": await sessions.get_diff(session_id)}


# ---------------------------------------------------------------------------
# Cursord facing API
# Used by the daemon inside the sandbox. Every call carries its epoch.
# ---------------------------------------------------------------------------


@app.post("/sandbox/{session_id}/register")
async def register_sandbox(session_id: str, body: RegisterRequest) -> Dict[str, Any]:
    registration = await sandbox.register(session_id, body.epoch, body.container_id)
    return dict({"ok": True}, **registration)


@app.get("/sandbox/{session_id}/next-action")
async def get_next_action(session_id: str, epoch: int = Query(..., ge=0)) -> Dict[str, Any]:
    """Long-poll for the pending tool call. A null tool means the hold expired."""
    action = await sessions.claim_next_action(session_id, epoch)
    if action is None:
        return {"ok": True, "tool": None}
    return {
        "ok": True,
        "tool": {
            "action_id": str(action.id),
            "name": action.name,
            "args": action.args,
            "attempt": action.attempts,
            "repeated": action.repeated,
        },
    }


@app.post("/sandbox/{session_id}/actions/{action_id}/result")
async def save_action_result(
    session_id: str, action_id: str, body: ActionResultRequest
) -> Dict[str, Any]:
    batch_complete = await sessions.record_action_result(
        session_id,
        action_id,
        epoch=body.epoch,
        result=body.result,
        exit_code=body.exit_code,
        commit_sha=body.commit_sha,
    )
    # Parallel calls: only the last result of the batch advances the loop.
    if batch_complete:
        await sessions.advance(session_id)
    return {"ok": True, "batch_complete": batch_complete}


@app.post("/sandbox/{session_id}/heartbeat")
async def heartbeat(session_id: str, body: HeartbeatRequest) -> Dict[str, Any]:
    return dict({"ok": True}, **await sandbox.heartbeat(session_id, body.epoch))
