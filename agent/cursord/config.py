"""Everything cursord is told. All of it comes from the environment."""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# handed in by the spawner (control/sandbox.py::_start_container)
# ---------------------------------------------------------------------------
# Defaulted rather than demanded, so importing a cursord module does not
# require a container's environment. require() demands them, once, at startup.
SESSION_ID = os.environ.get("SESSION_ID", "")
EPOCH = int(os.environ.get("EPOCH") or 0)
CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
REPO_URL = os.environ.get("REPO_URL", "")
BRANCH = os.environ.get("BRANCH", "")

REQUIRED = ("SESSION_ID", "EPOCH", "CONTROL_URL", "REPO_URL", "BRANCH")


def require() -> None:
    """Refuse to start without the environment the spawner is meant to pass."""
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("cursord: missing environment: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# the filesystem
# ---------------------------------------------------------------------------
# The clone. Tool paths resolve against it and are refused if they escape.
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/repo"))

# ---------------------------------------------------------------------------
# timings, all seconds
# ---------------------------------------------------------------------------
# Must stay well under the control plane's HEARTBEAT_DEATH (30s) or live
# sandboxes get replaced while they work. Three beats to the threshold.
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "10"))

# How long the control plane holds the next-action poll open. The read timeout
# has to exceed it or every poll looks like a network failure.
POLL_HOLD_SECONDS = float(os.environ.get("POLL_HOLD_SECONDS", "25"))
POLL_READ_TIMEOUT = POLL_HOLD_SECONDS + 10

RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 5.0

# How long the session may sit idle before this container gives up its seat.
IDLE_EXIT_SECONDS = float(os.environ.get("IDLE_EXIT_SECONDS", "300"))

# ---------------------------------------------------------------------------
# tool limits
# ---------------------------------------------------------------------------
COMMAND_TIMEOUT_SECONDS = float(os.environ.get("COMMAND_TIMEOUT", "120"))

# Oversized output is truncated in the middle, keeping head and tail.
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", str(64 * 1024)))
MAX_READ_BYTES = int(os.environ.get("MAX_READ_BYTES", str(256 * 1024)))
MAX_LIST_ENTRIES = 1000

# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
GIT_AUTHOR_NAME = "cursord"
GIT_AUTHOR_EMAIL = "cursord@local"

# Committed despite .gitignore, or they would not survive a rebuild.
FORCE_ADD_PATHS = tuple(
    p for p in os.environ.get("FORCE_ADD_PATHS", ".env").split(",") if p
)

# What the control plane is sent in place of the patch, which stays in the repo.
DIFF_PREVIEW_CHARS = int(os.environ.get("DIFF_PREVIEW_CHARS", str(16 * 1024)))
DIFF_MAX_FILES = int(os.environ.get("DIFF_MAX_FILES", "200"))
