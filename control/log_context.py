"""Structured logging with per-session correlation context."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

_FIELDS = ("session_id", "trace_id", "epoch", "action_id", "attempt")
_context = contextvars.ContextVar("control_log_context", default={})
_service = "control"
_secrets: tuple[str, ...] = ()


def _redact(value: str) -> str:
    for secret in _secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with stable correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        current = _context.get()
        session_id = getattr(record, "session_id", current.get("session_id"))
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "service": _service,
            "level": record.levelname,
            "logger": record.name,
            "message": _redact(record.getMessage()),
            "session_id": session_id,
            "trace_id": getattr(
                record, "trace_id", current.get("trace_id") or session_id
            ),
            "epoch": getattr(record, "epoch", current.get("epoch")),
            "action_id": getattr(record, "action_id", current.get("action_id")),
            "attempt": getattr(record, "attempt", current.get("attempt")),
        }
        for name in (
            "event",
            "duration_ms",
            "status_code",
            "error_category",
            "model",
            "tokens_in",
            "tokens_out",
            "context_chars",
            "message_count",
            "tool_count",
            "finish_reason",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure(service: str = "control") -> None:
    """Configure application loggers without adding duplicate handlers."""
    global _service, _secrets
    _service = service
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
def bind(**values: Any) -> Iterator[None]:
    """Temporarily add non-null values to every log record in this context."""
    merged = dict(_context.get())
    merged.update({key: value for key, value in values.items() if value is not None})
    if merged.get("session_id") and not merged.get("trace_id"):
        merged["trace_id"] = merged["session_id"]
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


def correlated(function: Callable) -> Callable:
    """Bind conventional session/epoch/action arguments for a function call."""
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        arguments = signature.bind_partial(*args, **kwargs).arguments
        values = {name: arguments.get(name) for name in _FIELDS}
        with bind(**values):
            return function(*args, **kwargs)

    return wrapper
