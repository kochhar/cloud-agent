"""Standalone, read-only operations dashboard."""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import FastAPI, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, queries
from .db import pool

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_pool=pool, collector=queries.collect_overview) -> FastAPI:
    """Build an app around an isolated pool; injection keeps integration tests real."""
    cache_lock = threading.Lock()
    cache: dict[int, tuple[float, dict[str, Any]]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db_pool.open()
        await anyio.to_thread.run_sync(db_pool.wait, 10)
        yield
        db_pool.close()

    application = FastAPI(title="cloud-agent-operations", lifespan=lifespan)

    @application.get("/healthz")
    def health() -> dict:
        with db_pool.connection() as conn:
            row = conn.execute(
                "SELECT current_database() AS database, now() AS checked_at"
            ).fetchone()
        return {"ok": True, **row}

    @application.get("/api/overview")
    def overview(hours: int = Query(24, ge=1, le=168)) -> dict:
        now = time.monotonic()
        with cache_lock:
            cached = cache.get(hours)
            if cached and now - cached[0] < config.CACHE_SECONDS:
                return cached[1]

        with db_pool.connection() as conn:
            snapshot = collector(conn, hours)

        with cache_lock:
            cache[hours] = (time.monotonic(), snapshot)
        return snapshot

    @application.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/ui/")

    application.mount(
        "/ui", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard"
    )
    return application


app = create_app()
