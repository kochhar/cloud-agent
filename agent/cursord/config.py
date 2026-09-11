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
# Read with defaults rather than demanded here, so that importing a cursord
# module does not require a container's environment. `require()` does the
# demanding, once, at startup. Without that split the tool registry cannot be
# imported by anything on the control-plane side, including the contract check
# that keeps the two halves of the tool surface in agreement.
SESSION_ID = os.environ.get("SESSION_ID", "")
EPOCH = int(os.environ.get("EPOCH") or 0)
CONTROL_URL = os.environ.get("CONTROL_URL", "").rstrip("/")
REPO_URL = os.environ.get("REPO_URL", "")
BRANCH = os.environ.get("BRANCH", "")

REQUIRED = ("SESSION_ID", "EPOCH", "CONTROL_URL", "REPO_URL", "BRANCH")


def require() -> None:
    """Refuse to start without the environment the spawner is meant to pass.

    Failing here is cheap and legible. Failing later means a container that
    registered, cloned, and then could not say where to push.
    """
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise SystemExit("cursord: missing environment: " + ", ".join(missing))

# ---------------------------------------------------------------------------
# the filesystem
# ---------------------------------------------------------------------------
# The clone. Every tool call resolves paths relative to this and is refused if
# it escapes. Nothing outside it is expected to survive the container.
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace/repo"))

# ---------------------------------------------------------------------------
# timings
# ---------------------------------------------------------------------------
# The interval and the control plane's death threshold are one decision made
# in two files: the reaper spawns a replacement the moment the threshold
# trips, so the threshold has to leave room for beats to go missing without
# the sandbox being wrong. Three misses is the rule of thumb, which puts the
# threshold at 90s for the 30s below. Raising this without raising that is
# how every live sandbox gets replaced while it is working.
#
# What the interval buys is how long a genuinely dead sandbox holds its
# session: a lower number finds the corpse sooner and costs a request per
# container per tick. 30s is the slower end of that trade, appropriate while
# the reaper is not built and nothing reads last_heartbeat_at yet.
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "30"))

# How long the control plane holds the next-action poll open. cursord's own
# read timeout has to exceed it or every poll looks like a network failure.
POLL_HOLD_SECONDS = float(os.environ.get("POLL_HOLD_SECONDS", "25"))
POLL_READ_TIMEOUT = POLL_HOLD_SECONDS + 10

# The control plane restarting is normal and not an error: the poll fails, we
# retry, another instance answers. Backoff keeps the retry from being a spin.
RETRY_BASE_SECONDS = 0.5
RETRY_MAX_SECONDS = 5.0

# How long the session may sit idle before this container gives up its seat.
# An idle session has finished its turn and is waiting on a person, which is
# an unbounded wait, and a container held open for it is a container the
# reaper will eventually mistake for a live one.
#
# The cost of leaving is a fresh clone when the conversation resumes; the
# cost of staying is a machine's worth of memory per abandoned tab. Five
# minutes is long enough that a reply typed straight back is still answered
# by this container, and short enough that a tab left open overnight is not.
#
# It lives here rather than on the control plane because the container is
# what it spends: the control plane would be deciding how long someone
# else's process should live.
IDLE_EXIT_SECONDS = float(os.environ.get("IDLE_EXIT_SECONDS", "300"))

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

# The control plane keeps no clone, so what it knows about the diff is what we
# send it. A whole patch has no bound worth relying on and would sit in a
# column that is rewritten on every commit, so it gets a preview and a
# per-file stat instead; the full patch stays in the repository, which is
# where the deliverable actually lives.
DIFF_PREVIEW_CHARS = int(os.environ.get("DIFF_PREVIEW_CHARS", str(16 * 1024)))
DIFF_MAX_FILES = int(os.environ.get("DIFF_MAX_FILES", "200"))
