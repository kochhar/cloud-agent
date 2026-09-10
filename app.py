"""Minimal FastAPI app that talks to the local Postgres instance."""

import os

import psycopg
from fastapi import FastAPI, HTTPException

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres@127.0.0.1:5432/project1",
)

app = FastAPI(title="project1")


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
