"""Minimal FastAPI app that talks to the local Postgres instance."""

import os

import psycopg
from fastapi import FastAPI, HTTPException

from control import sandbox, sessions
from db import DATABASE_URL


app = FastAPI(title="cloud-agent")


@app.get("/health")
def health():
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version(), current_database()")
                version, database = cur.fetchone()
    except psycopg.Error as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "database": database, "version": version}


### Client Facing Session API
### This API is used by the client to create new agent sessions and interact with them.
@app.post("/sessions")
def create_session():
    ## boilerplate code to extract URL and initial prompt from the request
    session = sessions.create_session(repo_url, initial_prompt)
    return {"ok": True, "session_id": session.id}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    session = sessions.get_session(session_id)
    return {"ok": True, "session": session}


@app.post("/sessions/{session_id}/messages")
def create_message(session_id: str, message: str):
    session = sessions.get_session(session_id)
    message = session.create_message(message)
    session.advance()
    return {"ok": True, "message_id": message.id}


@app.get("/sessions/{session_id}/events")
def get_events(session_id: str, seq: int = 0):
    events = sessions.get_events(session_id, seq)
    return {"ok": True, "events": events}


@app.post("/sessions/{session_id}/cancel")
def cancel_session(session_id: str):
    sessions.cancel_session(session_id)
    return {"ok": True}


@app.get("/sessions/{session_id}/diff")
def get_diff(session_id: str):
    diff = sessions.get_diff(session_id)
    return {"ok": True, "diff": diff}


### Cursord facing API
### This API is used by the cursord daemon to implement agent behaviours
@app.post("/sandbox/{session_id}/register")
def register_sandbox(session_id: str):
    sandbox = sandbox.register_sandbox(session_id, sandbox_id)
    return {
        "ok": True, 
        "repo_url": sandbox.session.repo_url, 
        "branch": sandbox.session.branch, 
        "base_sha": sandbox.session.base_sha
    }


@app.get("/sandbox/{session_id}/next-action")
def get_next_action(session_id: str):
    next_action = sessions.get_next_action(session_id)
    return {"ok": True, tool: next_action}


@app.post("/sandbox/{session_id}/actions/{action_id}/result")
def save_action_result(session_id: str, action_id: str):
    session = sessions.get_session(session_id)
    session.record_action_result(action_id, result)
    session.advance()
    return {"ok": True}


@app.post("/sandbox/{session_id}/heartbeat")
def heartbeat(session_id: str, epoch: int):
    status = session.heartbeat(session_id, epoch)
    return {"ok": True, "status": status}
