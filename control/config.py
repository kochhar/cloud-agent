"""Every knob the control plane has, in one place.

The server is started as a plain `uvicorn app:app`, with no shell step that
sources .env, so the file is read here instead. Real environment variables
always win: .env is the development default, not an override.

Modules import this and read `config.NAME` rather than binding the value at
import time, so a test that reassigns one gets the behaviour it asked for.
The exception is a default argument value, which Python evaluates once when
the function is defined no matter how it is spelled.
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
DATABASE_URL = _flag("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/project1")


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
# xAI speaks the OpenAI chat-completions dialect, so the path is the usual one.
GROK_BASE_URL = _flag("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_API_KEY: Optional[str] = os.environ.get("GROK_API_KEY") or None
GROK_MODEL = _flag("GROK_MODEL", "grok-4.6")

# A single turn can spend minutes reasoning before the first byte comes back,
# and advance() holds no lock while it waits, so this is generous on purpose.
GROK_TIMEOUT_SECONDS = float(_flag("GROK_TIMEOUT", "300"))
GROK_MAX_ATTEMPTS = int(_flag("GROK_MAX_ATTEMPTS", "4"))


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
# Shared by both long-polls: the events feed and the next-action poll. Held
# under the usual 30s proxy timeout so a poll returns rather than resets.
LONG_POLL_SECONDS = float(_flag("LONG_POLL_SECONDS", "25"))
POLL_INTERVAL_SECONDS = float(_flag("POLL_INTERVAL_SECONDS", "0.5"))

# Each sandbox death re-dispatches the same call. Past this many attempts the
# session fails rather than spawning containers forever.
MAX_TOOL_ATTEMPTS = int(_flag("MAX_TOOL_ATTEMPTS", "3"))

# Results reach the model in full and the UI truncated.
RESULT_PREVIEW_CHARS = int(_flag("RESULT_PREVIEW_CHARS", "2000"))


# ---------------------------------------------------------------------------
# sandbox: which runtime
# ---------------------------------------------------------------------------
# Which runtime starts cursord:
#
#   docker   one container per epoch, from SANDBOX_IMAGE. The real thing.
#   process  a plain subprocess on this machine. Development only, and
#            emphatically not a sandbox: see sandbox._start_process.
SANDBOX_RUNTIME = _flag("SANDBOX_RUNTIME", "docker")

SPAWN_TIMEOUT_SECONDS = float(_flag("SANDBOX_SPAWN_TIMEOUT", "60"))

GIT_TIMEOUT_SECONDS = float(_flag("SANDBOX_GIT_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# sandbox: docker runtime
# ---------------------------------------------------------------------------
# Without an image there is nothing to run; the sandbox row is still recorded
# so a cursord started by hand can register against the epoch.
SANDBOX_IMAGE = _flag("SANDBOX_IMAGE", "")

# How the container reaches this control plane. On Docker Desktop the host is
# not localhost from inside the container.
CONTROL_URL = _flag("SANDBOX_CONTROL_URL", "http://host.docker.internal:8000")


# ---------------------------------------------------------------------------
# sandbox: process runtime
# ---------------------------------------------------------------------------
# Where cursord is imported from, rather than baked into an image.
AGENT_DIR = Path(_flag("SANDBOX_AGENT_DIR", str(ROOT / "agent")))

# A local process reaches the control plane the ordinary way. host.docker
# .internal does not resolve outside a container.
LOCAL_CONTROL_URL = _flag("SANDBOX_LOCAL_CONTROL_URL", "http://127.0.0.1:8000")

# One clone per epoch, never reused: workspace.prepare() clones into this path
# and git refuses to clone into a directory that already has anything in it.
WORKSPACE_ROOT = Path(
    _flag(
        "SANDBOX_WORKSPACE_ROOT",
        os.path.join(tempfile.gettempdir(), "cursord-workspaces"),
    )
)

# The container runtime keeps logs; a subprocess does not, and a cursord that
# died on its first git call is otherwise silent.
LOG_ROOT = Path(
    _flag("SANDBOX_LOG_ROOT", os.path.join(tempfile.gettempdir(), "cursord-logs"))
)


# ---------------------------------------------------------------------------
# sandbox: git credentials for the container
# ---------------------------------------------------------------------------
# How the container authenticates to a real git remote.
#
#   agent  forward the host's ssh-agent socket. The key never enters the
#          container, so a model-authored `cat` cannot read it. The container
#          can still *use* the key while it runs, which is unavoidable if it
#          is to push at all.
#   keys   bind-mount the key directory read-only. Simpler, and strictly
#          worse: the private key is then a file inside a filesystem that
#          arbitrary model-authored commands can read and exfiltrate. Only
#          reasonable against a throwaway deploy key.
#   none   no credentials. Correct for the local bare repo, which is reached
#          by path rather than over the network.
SSH_MODE = _flag("SANDBOX_SSH_MODE", "agent")

SSH_DIR = os.path.expanduser(_flag("SANDBOX_SSH_DIR", "~/.ssh"))

# Docker Desktop exposes the host's agent at a fixed path inside the VM; there
# is no host socket to bind directly. On Linux the host socket is the real one.
DESKTOP_SSH_SOCK = "/run/host-services/ssh-auth.sock"
CONTAINER_SSH_SOCK = "/ssh-agent"
