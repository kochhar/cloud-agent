"""Sandbox lifecycle.

A container can vanish at any moment. What protects the session is the epoch:
it is bumped every time a sandbox is spawned, and cursord carries the epoch it
was born with on every request. A container that comes back from the dead has
an epoch behind the session's, so nothing it reports is accepted and it is
never handed more work.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import Any, Optional

from psycopg.rows import dict_row

import sessions
from db import pool

logger = logging.getLogger(__name__)

# Without an image there is nothing to run; the sandbox row is still recorded
# so a cursord started by hand can register against the epoch.
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "")

# How the container reaches this control plane. On Docker Desktop the host is
# not localhost from inside the container.
CONTROL_URL = os.environ.get("SANDBOX_CONTROL_URL", "http://host.docker.internal:8000")

SPAWN_TIMEOUT_SECONDS = float(os.environ.get("SANDBOX_SPAWN_TIMEOUT", "60"))


class SpawnRefused(Exception):
    """The session is not in a state that can take a new sandbox."""


def spawn(session_id: str) -> dict:
    """Open a new epoch for the session and start a container against it.

    The row is committed before the container is started, so a crash midway
    leaves a recoverable sandbox row rather than an orphaned container.
    """
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            # The UPDATE takes the row lock, so two instances spawning at once
            # get different epochs rather than the same one.
            cur.execute(
                "UPDATE sessions SET current_epoch = current_epoch + 1, updated_at = now() "
                "WHERE id = %s AND status NOT IN ('completed','failed','cancelled') "
                "RETURNING current_epoch, repo_url, branch",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise _refusal(cur, session_id)

            epoch = row["current_epoch"]
            repo_url = row["repo_url"]
            branch = row["branch"]

            cur.execute(
                "INSERT INTO sandboxes (session_id, epoch, status) "
                "VALUES (%s, %s, 'spawning') RETURNING id",
                (session_id, epoch),
            )
            sandbox_id = cur.fetchone()["id"]

            sessions._emit(cur, session_id, "sandbox_spawning", {"epoch": epoch})

    # TODO: handle a failed container start. _start_container returns None,
    # which strands the row at 'spawning' — a reaper that scans 'ready' rows
    # with expired heartbeats will never see it. Decide between marking the
    # sandbox 'dead' so the reaper retries it, and failing the session once
    # spawn attempts hit a ceiling.
    #
    # TODO: make the runtime an interface rather than a function. `docker run`
    # only works while the data plane shares a host with the control plane.
    # Same signature, two implementations behind a config switch: local docker,
    # and an HTTP call to a remote sandbox service that returns a container id.
    container_id = _start_container(session_id, epoch, repo_url, branch)
    if container_id:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE sandboxes SET container_id = %s WHERE id = %s",
                (container_id, sandbox_id),
            )

    return {
        "sandbox_id": sandbox_id,
        "epoch": epoch,
        "container_id": container_id,
        "branch": branch,
    }


def _refusal(cur, session_id: str) -> Exception:
    """Turn a no-op UPDATE into the reason it did not apply."""
    cur.execute("SELECT status FROM sessions WHERE id = %s", (session_id,))
    row = cur.fetchone()
    if row is None:
        return sessions.SessionNotFound(session_id)
    return SpawnRefused("session {} is {}".format(session_id, row["status"]))


def _start_container(
    session_id: str, epoch: int, repo_url: str, branch: str
) -> Optional[str]:
    """Run cursord in a container. Returns the container id, or None.

    Returning None is not fatal: the sandbox row exists at this epoch, so a
    cursord process started by hand can register against it.

    This is the local-host implementation, and the one that becomes an
    interface per the TODO in spawn().
    """
    if not SANDBOX_IMAGE:
        logger.warning(
            "SANDBOX_IMAGE is unset; session %s epoch %s has no container",
            session_id,
            epoch,
        )
        return None

    if shutil.which("docker") is None:
        logger.warning("docker is not on PATH; session %s epoch %s has no container",
                       session_id, epoch)
        return None

    command = [
        "docker", "run", "--detach", "--rm",
        "--env", "SESSION_ID={}".format(session_id),
        "--env", "EPOCH={}".format(epoch),
        "--env", "CONTROL_URL={}".format(CONTROL_URL),
        "--env", "REPO_URL={}".format(repo_url),
        "--env", "BRANCH={}".format(branch),
    ]
    if os.path.isdir(repo_url):
        # The bare repo stands in for GitHub, so the container needs it mounted.
        command += ["--volume", "{}:{}".format(repo_url, repo_url)]
    command.append(SANDBOX_IMAGE)

    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=SPAWN_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        logger.error(
            "docker run failed for session %s epoch %s: %s",
            session_id,
            epoch,
            stderr.strip() or exc,
        )
        return None

    return finished.stdout.strip()[:12] or None
