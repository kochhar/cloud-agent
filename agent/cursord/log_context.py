"""Structured daemon logging with stable session and epoch correlation."""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

_context = contextvars.ContextVar("cursord_log_context", default={})
_secrets: tuple[str, ...] = ()


def _redact(value: str) -> str:
    for secret in _secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        current = _context.get()
        session_id = config.SESSION_ID or None
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "service": "cursord",
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
            "session_id": session_id,
            "trace_id": session_id,
            "epoch": config.EPOCH,
            "action_id": current.get("action_id"),
            "attempt": current.get("attempt"),
        }
        for name in (
            "event",
            "duration_ms",
            "status_code",
            "exit_code",
            "error_category",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure() -> None:
    global _secrets
    _secrets = tuple(
        value
        for name, value in os.environ.items()
        if value
        and len(value) >= 8
        and any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))
    )
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
    root.setLevel(logging.INFO)


@contextlib.contextmanager
def bind(action_id: str | None = None, attempt: int | None = None) -> Iterator[None]:
    merged = dict(_context.get())
    if action_id is not None:
        merged["action_id"] = action_id
    if attempt is not None:
        merged["attempt"] = attempt
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)
