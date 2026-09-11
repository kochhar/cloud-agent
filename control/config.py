"""Every knob the control plane has, in one place.

.env is read here because the server starts as a plain `uvicorn app:app` with
no shell step to source it. Real environment variables win.

Read as `config.NAME` at call time so tests can reassign. Default argument
values are the exception, since Python evaluates those once at definition.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

# This file lives in control/; the repository root is one level up.
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()

        # Quotes are a shell artifact and are not part of the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        os.environ.setdefault(name, value)


_load_env_file(ENV_FILE)


def _flag(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
# libpq connection string.
DATABASE_URL = _flag("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/project1")


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
# Base URL of an OpenAI-compatible /chat/completions API.
GROK_BASE_URL = _flag("GROK_BASE_URL", "https://api.x.ai/v1")

# Unset means no provider: llm.use() has to install a client or turns fail.
GROK_API_KEY: Optional[str] = os.environ.get("GROK_API_KEY") or None

GROK_MODEL = _flag("GROK_MODEL", "grok-4.6")

# xAI grok-4.6 card: $2 / $6 per million below 200k prompt tokens, doubled at
# or above that threshold for the whole request. Override when the bill changes.
GROK_INPUT_COST_PER_MILLION = float(_flag("GROK_INPUT_COST_PER_MILLION", "2"))
GROK_OUTPUT_COST_PER_MILLION = float(_flag("GROK_OUTPUT_COST_PER_MILLION", "6"))
GROK_LONG_CONTEXT_TOKENS = int(_flag("GROK_LONG_CONTEXT_TOKENS", "200000"))
GROK_LONG_CONTEXT_MULTIPLIER = float(_flag("GROK_LONG_CONTEXT_MULTIPLIER", "2"))

# Seconds per HTTP attempt. advance() holds no lock while it waits, so a large
# value costs a parked thread and nothing else. thinking_since is refreshed at
# the start of each attempt, so this is also the bound a live turn needs.
GROK_TIMEOUT_SECONDS = float(_flag("GROK_TIMEOUT", "240"))

# Attempts per turn, including the first. Retries no longer stretch
# thinking_deadline(); each attempt resets the clock.
GROK_MAX_ATTEMPTS = int(_flag("GROK_MAX_ATTEMPTS", "4"))


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
# Seconds a long-poll is held, on both the event feed and the next-action
# poll. Keep below the proxy idle timeout (commonly 30) or polls are reset
# rather than returned.
LONG_POLL_SECONDS = float(_flag("LONG_POLL_SECONDS", "25"))

# Seconds between database checks inside a held poll. Lower dispatches tool
# calls sooner and costs more queries per waiting sandbox.
POLL_INTERVAL_SECONDS = float(_flag("POLL_INTERVAL_SECONDS", "0.5"))

# Dispatches of a single tool call before that call is marked failed and
# handed back to the model. Counts sandbox deaths, not command exit codes:
# a command that fails is a result, not a retry. The session stays
# executing so the rest of the batch can close.
MAX_TOOL_ATTEMPTS = int(_flag("MAX_TOOL_ATTEMPTS", "3"))


# ---------------------------------------------------------------------------
# the nudger
# ---------------------------------------------------------------------------
# Seconds between passes. A latency knob: passes are idempotent.
NUDGE_INTERVAL_SECONDS = float(_flag("NUDGE_INTERVAL", "15"))

# Seconds without a heartbeat before a 'ready' sandbox is replaced. Keep well
# above cursord's HEARTBEAT_INTERVAL (10s), or one dropped beat replaces a
# healthy sandbox.
HEARTBEAT_DEATH_SECONDS = float(_flag("HEARTBEAT_DEATH", "30"))

# Seconds at 'spawning' before the start counts as failed. Keep above
# SPAWN_TIMEOUT_SECONDS, which bounds the runtime call itself.
SPAWN_STUCK_SECONDS = float(_flag("SPAWN_STUCK", "120"))

# Sandboxes a session may lose before it fails. Deaths only, so an ordinary
# resume does not spend the budget.
MAX_SANDBOX_LOSSES = int(_flag("MAX_SANDBOX_LOSSES", "10"))

# Seconds before a nudge advances a turn nobody picked up. Only avoids racing
# the caller that normally would.
ADVANCE_GRACE_SECONDS = float(_flag("ADVANCE_GRACE", "30"))

# Model calls one instance runs for the nudger at once, each holding a thread
# for a whole turn. Per process, not per cluster: three instances cap at
# 3 × this, all racing _claim_thinking. Sessions over the cap wait for a
# later pass.
NUDGE_MAX_ADVANCES = int(_flag("NUDGE_MAX_ADVANCES", "8"))

# Slack on top of one HTTP attempt. thinking_since moves at the start of
# each attempt, so a live retry is indistinguishable from a first try.
THINKING_DEADLINE_SLACK_SECONDS = 30.0


def thinking_deadline() -> float:
    """Seconds since the last thinking_since bump before a nudge treats the
    caller as gone.

    One HTTP attempt plus slack, not attempts × timeout: a live process
    refreshes thinking_since when it starts each try. Too low and the nudge
    interrupts that try and runs the model twice on one context.
    """
    return GROK_TIMEOUT_SECONDS + THINKING_DEADLINE_SLACK_SECONDS


# ---------------------------------------------------------------------------
# payload limits
# ---------------------------------------------------------------------------
# Chars of tool output put on the event feed. The model still gets the result
# in full; this only bounds what the UI is sent.
RESULT_PREVIEW_CHARS = int(_flag("RESULT_PREVIEW_CHARS", "2000"))

# Chars of patch kept in sessions.diff_preview, which is rewritten on every
# commit. Applied to what the sandbox sends, so its own cap cannot grow this
# column. The full patch stays in the repository.
DIFF_PREVIEW_CHARS = int(_flag("DIFF_PREVIEW_CHARS", str(16 * 1024)))

# host -> two-dot compare URL template. A host that is absent here is not an
# error; get_diff just returns url: null for it.
COMPARE_URLS = {
    "github.com": "https://github.com/{repo}/compare/{base}...{head}",
    "gitlab.com": "https://gitlab.com/{repo}/-/compare/{base}...{head}",
    "bitbucket.org": "https://bitbucket.org/{repo}/branches/compare/{head}..{base}",
}


# ---------------------------------------------------------------------------
# sandbox: which runtime
# ---------------------------------------------------------------------------
# Which runtime starts cursord:
#
#   docker   one container per epoch, from SANDBOX_IMAGE. The real thing.
#   process  a plain subprocess on this machine. Development only, and not a
#            sandbox: model-authored commands run as this user, with this
#            user's filesystem, ssh keys and database access.
SANDBOX_RUNTIME = _flag("SANDBOX_RUNTIME", "process")

# Seconds to wait for the runtime to hand back a container id.
SPAWN_TIMEOUT_SECONDS = float(_flag("SANDBOX_SPAWN_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# sandbox: docker runtime
# ---------------------------------------------------------------------------
# Empty means no container is started. The sandbox row is written anyway, so a
# cursord started by hand can register against the epoch.
SANDBOX_IMAGE = _flag("SANDBOX_IMAGE", "")

# How the container reaches this control plane. Not localhost from inside a
# container; on Docker Desktop the host is host.docker.internal.
CONTROL_URL = _flag("SANDBOX_CONTROL_URL", "http://host.docker.internal:8000")


# ---------------------------------------------------------------------------
# sandbox: process runtime
# ---------------------------------------------------------------------------
# Where cursord is imported from, rather than baked into an image.
AGENT_DIR = Path(_flag("SANDBOX_AGENT_DIR", str(ROOT / "agent")))

# How a local subprocess reaches the control plane. host.docker.internal does
# not resolve outside a container.
LOCAL_CONTROL_URL = _flag("SANDBOX_LOCAL_CONTROL_URL", "http://127.0.0.1:8000")

# One clone per epoch, never reused: git refuses to clone into a non-empty
# directory.
WORKSPACE_ROOT = Path(
    _flag(
        "SANDBOX_WORKSPACE_ROOT",
        os.path.join(tempfile.gettempdir(), "cursord-workspaces"),
    )
)

# Where a subprocess's stdout goes. A container runtime keeps its own logs; a
# subprocess does not, and a cursord that died on its first git call is
# otherwise silent.
LOG_ROOT = Path(
    _flag("SANDBOX_LOG_ROOT", os.path.join(tempfile.gettempdir(), "cursord-logs"))
)


# ---------------------------------------------------------------------------
# sandbox: git credentials for the container
# ---------------------------------------------------------------------------
# How the container authenticates to a git remote:
#
#   agent  forward the host's ssh-agent socket. The key never enters the
#          container, so a model-authored `cat` cannot read it, though the
#          container can still use it while running.
#   keys   bind-mount SSH_DIR read-only. The private key becomes a readable
#          file in a filesystem where arbitrary model-authored commands run,
#          so only reasonable with a throwaway deploy key.
#
# There is no credential-free mode: every repo is reached over the network and
# a checkpoint is a push.
SSH_MODE = _flag("SANDBOX_SSH_MODE", "agent")

SSH_DIR = os.path.expanduser(_flag("SANDBOX_SSH_DIR", "~/.ssh"))

# Docker Desktop exposes the host agent at a fixed path inside its VM; there is
# no host socket to bind. On Linux the host socket is the real one.
DESKTOP_SSH_SOCK = "/run/host-services/ssh-auth.sock"
CONTAINER_SSH_SOCK = "/ssh-agent"
