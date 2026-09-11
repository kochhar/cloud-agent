"""Sandbox lifecycle.

A container can vanish at any moment. The session is protected by the epoch:
it is bumped every time a sandbox is spawned, and cursord carries the epoch when it
was born. A container that comes back from the dead has an epoch behind the session's, 
so nothing it reports is accepted and it is never handed more work.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Any, Optional

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

GIT_TIMEOUT_SECONDS = float(os.environ.get("SANDBOX_GIT_TIMEOUT", "30"))

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
SSH_MODE = os.environ.get("SANDBOX_SSH_MODE", "agent")

SSH_DIR = os.path.expanduser(os.environ.get("SANDBOX_SSH_DIR", "~/.ssh"))

# Docker Desktop exposes the host's agent at a fixed path inside the VM; there
# is no host socket to bind directly. On Linux the host socket is the real one.
DESKTOP_SSH_SOCK = "/run/host-services/ssh-auth.sock"
CONTAINER_SSH_SOCK = "/ssh-agent"


class SpawnRefused(Exception):
    """The session is not in a state that can take a new sandbox."""


def spawn(session_id: str) -> dict:
    """Open a new epoch for the session and start a container against it.

    The row is committed before the container is started, so a crash midway
    leaves a recoverable sandbox row rather than an orphaned container.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
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
                # Either the id is unknown or the session is already finished.
                # Not worth a second query to tell them apart: the only caller
                # is create_session, which just inserted the row, so in
                # practice this is always a terminal session.
                raise SpawnRefused(
                    f"session {session_id} cannot take a new sandbox"
                )

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


def register(session_id: str, epoch: int, container_id: str | None = None) -> dict:
    """A sandbox announces itself. Returns what it needs to clone and check out.

    Called once per epoch, either by the container spawn() started or by a
    cursord run by hand against the same epoch. Re-registering is harmless:
    a container that restarts inside its epoch lands on the same row.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            current = sessions._require_current_epoch(cur, session_id, epoch)
            if epoch > current:
                # Not stale but nonsense: no sandbox exists at an epoch the
                # session has not reached.
                raise SpawnRefused(
                    f"epoch {epoch} is ahead of session {session_id} at epoch {current}"
                )

            cur.execute(
                "SELECT repo_url, branch, base_sha, last_accepted_sha "
                "FROM sessions WHERE id = %s",
                (session_id,),
            )
            session = cur.fetchone()

            # Resolved once, on the first registration, and left alone after
            # that: the diff has to sit against where the work started, not
            # against wherever the remote has moved to since.
            base_sha = session["base_sha"] or _resolve_base_sha(session["repo_url"])
            if base_sha and base_sha != session["base_sha"]:
                cur.execute(
                    "UPDATE sessions SET base_sha = %s, updated_at = now() WHERE id = %s",
                    (base_sha, session_id),
                )

            cur.execute(
                """
                INSERT INTO sandboxes
                    (session_id, epoch, container_id, status, last_heartbeat_at)
                VALUES (%s, %s, %s, 'ready', now())
                ON CONFLICT (session_id, epoch) DO UPDATE
                    SET status = 'ready',
                        container_id = COALESCE(
                            EXCLUDED.container_id, sandboxes.container_id),
                        last_heartbeat_at = now()
                """,
                (session_id, epoch, container_id),
            )

            sessions._emit(cur, session_id, "sandbox_ready", {"epoch": epoch})

    return {
        "repo_url": session["repo_url"],
        "branch": session["branch"],
        "base_sha": base_sha,
        # A replacement resumes from the last SHA the control plane accepted,
        # not from whatever the dead sandbox managed to push before it went.
        "resume_sha": session["last_accepted_sha"],
        "epoch": epoch,
    }


def _resolve_base_sha(repo_url: str) -> Optional[str]:
    """The head the work starts from, so there is something to diff against."""
    if os.path.isdir(repo_url):
        command = ["git", "-C", repo_url, "rev-parse", "HEAD"]
    else:
        command = ["git", "ls-remote", repo_url, "HEAD"]

    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        # Not fatal: registration still succeeds, but the diff will be empty
        # until a base is known.
        logger.warning(
            "could not resolve base sha for %s: %s",
            repo_url,
            (getattr(exc, "stderr", "") or "").strip() or exc,
        )
        return None

    fields = finished.stdout.split()
    return fields[0] if fields else None


def _ssh_arguments() -> list:
    """Docker arguments giving the container git access to a real remote.

    Known hosts are mounted in every mode. Without them `StrictHostKeyChecking
    =yes` refuses the connection, and with checking turned off instead the
    container would accept any host key for the remote it pushes to.
    """
    if SSH_MODE == "none":
        return []

    if SSH_MODE == "keys":
        # The whole directory, known_hosts included. The private key becomes
        # readable by every command the model runs.
        logger.warning(
            "SANDBOX_SSH_MODE=keys mounts %s into a container that executes "
            "model-authored commands; the key is readable there",
            SSH_DIR,
        )
        return ["--volume", "{}:/root/.ssh:ro".format(SSH_DIR)]

    if SSH_MODE != "agent":
        raise ValueError("unknown SANDBOX_SSH_MODE {!r}".format(SSH_MODE))

    arguments = []

    known_hosts = os.path.join(SSH_DIR, "known_hosts")
    if os.path.isfile(known_hosts):
        arguments += [
            "--volume", "{}:/root/.ssh/known_hosts:ro".format(known_hosts),
        ]
    else:
        # Worth saying out loud: the failure is a clone that cannot verify the
        # host, which reads as a permissions problem rather than a missing file.
        logger.warning(
            "no known_hosts at %s; the container cannot verify the git remote. "
            "Run: ssh-keyscan github.com >> %s",
            known_hosts,
            known_hosts,
        )

    host_sock = _host_ssh_sock()
    if host_sock is None:
        # Not fatal here: a clone from a reachable remote may still work, and
        # failing the spawn would hide the real error behind a spawn refusal.
        logger.warning(
            "SANDBOX_SSH_MODE=agent but no ssh-agent socket found; "
            "the container will have no git credentials"
        )
        return arguments

    return arguments + [
        "--volume", "{}:{}".format(host_sock, CONTAINER_SSH_SOCK),
        "--env", "SSH_AUTH_SOCK={}".format(CONTAINER_SSH_SOCK),
    ]


def _host_ssh_sock() -> Optional[str]:
    """The socket to forward, or None if the host has no agent running.

    On Docker Desktop the host's `SSH_AUTH_SOCK` is not reachable from the
    VM, so the fixed path it publishes is used instead. It only carries keys
    the user has actually added, so `ssh-add` having been run is part of the
    contract either way.
    """
    if sys.platform == "darwin":
        return DESKTOP_SSH_SOCK

    sock = os.environ.get("SSH_AUTH_SOCK")
    if sock and os.path.exists(sock):
        return sock
    return None


def _start_container(
    session_id: str, epoch: int, repo_url: str, branch: str
) -> Optional[str]:
    """Run cursord in a container. Returns the container id, or None.

    Returning None is not fatal: the sandbox row exists at this epoch, so a
    cursord process started by hand can register against it.
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
    else:
        # Anything else is a real remote and needs credentials to read it,
        # and the same credentials again to push every checkpoint.
        command += _ssh_arguments()

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
