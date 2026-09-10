"""Everything cursord is told, and everything it is allowed to assume.

The container is handed five things by `docker run` and learns the rest from
the register call. Nothing here is read from a file, because a config file
would be one more thing to keep alive across a rebuild.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# handed in by the spawner (control/sandbox.py::_start_container)
# ---------------------------------------------------------------------------
SESSION_ID = os.environ["SESSION_ID"]
EPOCH = int(os.environ["EPOCH"])
CONTROL_URL = os.environ["CONTROL_URL"].rstrip("/")
REPO_URL = os.environ["REPO_URL"]
BRANCH = os.environ["BRANCH"]

# ---------------------------------------------------------------------------
# the filesystem
# ---------------------------------------------------------------------------
# The clone. Every tool call resolves paths relative to this and is refused if
# it escapes. Nothing outside it is expected to survive the container.
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/repo"))

# ---------------------------------------------------------------------------
# timings
# ---------------------------------------------------------------------------
# Heartbeats have to be well inside the control plane's death threshold (10s
# in this build) because the reaper spawns a replacement the moment it trips.
# Three seconds gives us three misses before anyone panics.
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "3"))

# How long the control plane holds the next-action poll open. cursord's own
# read timeout has to exceed it or every poll looks like a network failure.
POLL_HOLD_SECONDS = float(os.environ.get("POLL_HOLD_SECONDS", "25"))
POLL_READ_TIMEOUT = POLL_HOLD_SECONDS + 10

# The control plane restarting is normal and not an error: the poll fails, we
# retry, another instance answers. Backoff keeps the retry from being a spin.
RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 5.0

# ---------------------------------------------------------------------------
# tool limits
# ---------------------------------------------------------------------------
# A command that never returns would otherwise hold the only work slot the
# session has, and the session would look alive while making no progress.
COMMAND_TIMEOUT_SECONDS = float(os.environ.get("COMMAND_TIMEOUT", "120"))

# Results are a column in Postgres and a slice of the model's context window.
# Oversized output is truncated in the middle, keeping head and tail.
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(64 * 1024)))
MAX_READ_BYTES = int(os.environ.get("MAX_READ_BYTES", str(256 * 1024)))
MAX_LIST_ENTRIES = 1000

# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
# Commits are checkpoints, not history. The identity only has to be valid.
GIT_AUTHOR_NAME = "cursord"
GIT_AUTHOR_EMAIL = "cursord@local"

# Files matching .gitignore never reach the bare repo, so they do not survive a
# rebuild. `.env` is the case that actually bites. Force-adding a small
# allowlist covers it; the general problem is a known limitation.
FORCE_ADD_PATHS = tuple(
    p for p in os.environ.get("FORCE_ADD_PATHS", ".env").split(",") if p
)
