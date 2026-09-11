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
from typing import Optional

import config
import sessions
from db import pool

logger = logging.getLogger(__name__)


class SpawnRefused(Exception):
    """The session is not in a state that can take a new sandbox."""


class SandboxNotRegistered(Exception):
    """A heartbeat arrived for an epoch that has no sandbox row."""


# Reasons a sandbox can stop with nothing wrong. Anything else it reports on
# the way out is a sandbox that stopped early, which is the reaper's problem
# even though this one was polite enough to say so. The sandbox reports what
# happened; deciding whether that needs a replacement is not its call.
CLEAN_EXITS = frozenset({"session_finished"})


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
                "WHERE id = %s AND status NOT IN ('failed','cancelled') "
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

    # TODO: handle a failed start. The runtime returns None, which strands the
    # row at 'spawning' — a reaper that scans 'ready' rows with expired
    # heartbeats will never see it. Decide between marking the sandbox 'dead'
    # so the reaper retries it, and failing the session once spawn attempts
    # hit a ceiling.
    #
    # TODO: a third runtime, for a data plane that does not share a host with
    # the control plane: an HTTP call to a remote sandbox service that returns
    # a handle. Same signature as the two below.
    container_id = _start_sandbox(session_id, epoch, repo_url, branch)
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
        # Null on a first spawn. Nothing here resolves it: the control plane
        # runs no git at all, so the base is whatever the sandbox finds when
        # it clones, and it comes back with the sandbox's first result. A
        # rebuild reads it from here so its diff still starts where the work
        # did rather than at the point it resumed from.
        "base_sha": session["base_sha"],
        # A replacement resumes from the last SHA the control plane accepted,
        # not from whatever the dead sandbox managed to push before it went.
        "resume_sha": session["last_accepted_sha"],
        "epoch": epoch,
    }


def heartbeat(
    session_id: str,
    epoch: int,
    exiting: bool = False,
    reason: str | None = None,
) -> dict:
    """Liveness, and the one place a sandbox announces its own shutdown.

    A stale epoch raises, which is what tells a zombie to stop: it is the
    only failure a sandbox is meant to die on, so nothing else in here may
    surface as a 409.

    Ordinary beats do not write to the event log. At one every three seconds
    they would bury the transcript in noise that says nothing happened; the
    timestamp on the row is the whole record. An exit is different, because
    it happens once and explains why the sandbox stopped.
    """
    if exiting:
        status = "exited" if reason in CLEAN_EXITS else "dead"
    else:
        status = "ready"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            sessions._require_current_epoch(cur, session_id, epoch)

            cur.execute(
                """
                UPDATE sandboxes
                   SET last_heartbeat_at = now(),
                       status = CASE WHEN status = 'exited'
                                     THEN 'exited' ELSE %s END
                 WHERE session_id = %s AND epoch = %s
                RETURNING status
                """,
                # 'exited' is final. A beat still in flight when the container
                # said goodbye must not land it back in the reaper's index,
                # where a clean shutdown reads as a crash ten seconds later.
                #
                # 'dead' is not final. A beat from a sandbox the reaper gave
                # up on is proof the reaper was wrong, and the epoch check
                # above means no replacement has taken over yet.
                (status, session_id, epoch),
            )
            row = cur.fetchone()
            if row is None:
                # Not a 409: on this endpoint that means "you are stale, stop
                # working", and a sandbox that never registered would take
                # itself down for a reason that has nothing to do with it.
                raise SandboxNotRegistered(
                    f"session {session_id} has no sandbox at epoch {epoch}; "
                    "register before sending heartbeats"
                )

            if exiting:
                sessions._emit(
                    cur,
                    session_id,
                    "sandbox_exited",
                    {
                        "epoch": epoch,
                        "reason": reason or "unspecified",
                        "status": row["status"],
                    },
                )

    if exiting:
        logger.info(
            "sandbox for session %s epoch %s exited (%s), marked %s",
            session_id,
            epoch,
            reason or "unspecified",
            row["status"],
        )

    return {"epoch": epoch, "status": row["status"]}


def _ssh_arguments() -> list:
    """Docker arguments giving the container git access to a real remote.

    Known hosts are mounted in every mode. Without them `StrictHostKeyChecking
    =yes` refuses the connection, and with checking turned off instead the
    container would accept any host key for the remote it pushes to.
    """
    if config.SSH_MODE == "none":
        return []

    if config.SSH_MODE == "keys":
        # The whole directory, known_hosts included. The private key becomes
        # readable by every command the model runs.
        logger.warning(
            "SANDBOX_SSH_MODE=keys mounts %s into a container that executes "
            "model-authored commands; the key is readable there",
            config.SSH_DIR,
        )
        return ["--volume", "{}:/root/.ssh:ro".format(config.SSH_DIR)]

    if config.SSH_MODE != "agent":
        raise ValueError("unknown SANDBOX_SSH_MODE {!r}".format(config.SSH_MODE))

    arguments = []

    known_hosts = os.path.join(config.SSH_DIR, "known_hosts")
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
        "--volume", "{}:{}".format(host_sock, config.CONTAINER_SSH_SOCK),
        "--env", "SSH_AUTH_SOCK={}".format(config.CONTAINER_SSH_SOCK),
    ]


def _host_ssh_sock() -> Optional[str]:
    """The socket to forward, or None if the host has no agent running.

    On Docker Desktop the host's `SSH_AUTH_SOCK` is not reachable from the
    VM, so the fixed path it publishes is used instead. It only carries keys
    the user has actually added, so `ssh-add` having been run is part of the
    contract either way.
    """
    if sys.platform == "darwin":
        return config.DESKTOP_SSH_SOCK

    sock = os.environ.get("SSH_AUTH_SOCK")
    if sock and os.path.exists(sock):
        return sock
    return None


def _start_sandbox(
    session_id: str, epoch: int, repo_url: str, branch: str
) -> Optional[str]:
    """Start cursord on whichever runtime is configured.

    Every runtime has the same contract: hand cursord the five environment
    values it demands, return a handle for the sandboxes row, and return None
    rather than raise if it could not start.
    """
    runtime = _RUNTIMES.get(config.SANDBOX_RUNTIME)
    if runtime is None:
        raise ValueError(
            "unknown SANDBOX_RUNTIME {!r}; expected one of {}".format(
                config.SANDBOX_RUNTIME, ", ".join(sorted(_RUNTIMES))
            )
        )
    return runtime(session_id, epoch, repo_url, branch)


def _cursord_env(
    session_id: str, epoch: int, repo_url: str, branch: str, control_url: str
) -> dict:
    """The five values config.require() refuses to start without.

    Shared by both runtimes so that adding one to the container and
    forgetting it here is not a way to fail.
    """
    return {
        "SESSION_ID": session_id,
        "EPOCH": str(epoch),
        "CONTROL_URL": control_url,
        "REPO_URL": repo_url,
        "BRANCH": branch,
    }


# Popen objects for sandboxes this instance started, so an exited one is
# waited on rather than left a zombie. Purely housekeeping: the handle in the
# sandboxes row is a pid, so any instance can signal any sandbox without this.
_PROCESSES: dict = {}


def _reap_exited() -> None:
    for key, process in list(_PROCESSES.items()):
        if process.poll() is not None:
            logger.info("cursord %s exited with %s", key, process.returncode)
            del _PROCESSES[key]


def _start_process(
    session_id: str, epoch: int, repo_url: str, branch: str
) -> Optional[str]:
    """Run cursord as a subprocess on this machine. Development only.

    This is not a sandbox. run_command executes as this user, on this
    filesystem, with this user's ssh keys and this user's access to the
    control plane's own database. It exists so the loop can be driven
    end-to-end against a local bare repo without Docker, and the model in
    that setup is one you are watching.
    """
    _reap_exited()

    if not (config.AGENT_DIR / "cursord").is_dir():
        logger.error(
            "no cursord package under %s; set SANDBOX_AGENT_DIR", config.AGENT_DIR
        )
        return None

    # Per epoch, and cleared first: a rebuild must clone fresh, and prepare()
    # clones into this path.
    workspace = config.WORKSPACE_ROOT / session_id / str(epoch)
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.parent.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        _cursord_env(session_id, epoch, repo_url, branch, config.LOCAL_CONTROL_URL)
    )
    env["WORKSPACE"] = str(workspace)
    env["PYTHONPATH"] = str(config.AGENT_DIR)
    env["PYTHONUNBUFFERED"] = "1"
    # Same reasoning as the Dockerfile: without BatchMode a missing key or an
    # unknown host becomes a prompt on a terminal nobody is attached to, and
    # the clone hangs instead of failing with a reason.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

    config.LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_ROOT / "{}.{}.log".format(session_id, epoch)

    command = [
        "/bin/bash",
        "-c",
        # $$ is this shell's pid and exec replaces the shell, so cursord runs
        # as the pid recorded below and reports the same handle back from
        # register(). Left alone it falls back to the machine hostname, which
        # would overwrite the pid with a value nothing can signal.
        'HOSTNAME="local:$$" exec "$0" -m cursord',
        sys.executable,
    ]

    try:
        with open(log_path, "ab") as log:
            process = subprocess.Popen(
                command,
                cwd=str(config.AGENT_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                # Its own session, so a control plane restart does not take
                # the sandbox down with it.
                start_new_session=True,
            )
    except OSError as exc:
        logger.error(
            "could not start cursord for session %s epoch %s: %s",
            session_id,
            epoch,
            exc,
        )
        return None

    _PROCESSES["{}:{}".format(session_id, epoch)] = process
    logger.info(
        "cursord pid %s for session %s epoch %s, logging to %s",
        process.pid,
        session_id,
        epoch,
        log_path,
    )
    return "local:{}".format(process.pid)


def _start_container(
    session_id: str, epoch: int, repo_url: str, branch: str
) -> Optional[str]:
    """Run cursord in a container. Returns the container id, or None.

    Returning None is not fatal: the sandbox row exists at this epoch, so a
    cursord process started by hand can register against it.
    """
    if not config.SANDBOX_IMAGE:
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

    command = ["docker", "run", "--detach", "--rm"]
    for name, value in _cursord_env(
        session_id, epoch, repo_url, branch, config.CONTROL_URL
    ).items():
        command += ["--env", "{}={}".format(name, value)]

    if os.path.isdir(repo_url):
        # The bare repo stands in for GitHub, so the container needs it mounted.
        command += ["--volume", "{}:{}".format(repo_url, repo_url)]
    else:
        # Anything else is a real remote and needs credentials to read it,
        # and the same credentials again to push every checkpoint.
        command += _ssh_arguments()

    command.append(config.SANDBOX_IMAGE)

    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=config.SPAWN_TIMEOUT_SECONDS,
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


# Declared after both, so the table holds the functions rather than their names.
_RUNTIMES = {
    "docker": _start_container,
    "process": _start_process,
}
