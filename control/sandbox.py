"""Sandbox lifecycle.

A container can vanish at any moment, so the epoch fences it: bumped on every
spawn, and carried by cursord from its birth. A container that comes back from
the dead is behind the session, so nothing it reports is accepted and it is
never handed more work.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

import config
import log_context
import sessions
from db import pool
from models import FAILED, TERMINAL_STATUSES

logger = logging.getLogger(__name__)


class SpawnRefused(Exception):
    """The session is not in a state that can take a new sandbox."""


class SandboxNotRegistered(Exception):
    """A heartbeat arrived for an epoch that has no sandbox row."""


# Reasons a sandbox can stop with nothing wrong, matching what cursord sends:
#
#   idle              the turn ended and nobody came back
#   session_finished  the session reached 'failed' or 'cancelled'
#
# Anything else it reports is treated as a crash. 'idle' must be here: reading
# it as a crash would replace a sandbox that left because there was no work,
# which would go idle and leave again.
CLEAN_EXITS = frozenset({"idle", "session_finished"})

# Sandbox statuses that can still serve a session. Anything else has stopped.
LIVE_STATUSES = ("spawning", "ready")


@log_context.correlated
def spawn(
    session_id: str,
    expect_epoch: int | None = None,
    reason: str | None = None,
) -> dict:
    """Open a new epoch for the session and start a container against it.

    The row is committed before the container starts, so a crash midway leaves
    a recoverable sandbox row rather than an orphaned container.

    `expect_epoch` makes replacement exactly-once: it turns the bump into a
    compare-and-swap, so several instances finding the same dead sandbox
    produce one replacement between them. None from create_session, which has
    no previous epoch to compare against.

    `reason` is why the previous sandbox is being replaced, recorded on the
    event feed. None for a first spawn.
    """
    # Set when the epoch ceiling is hit. The write goes inside the transaction
    # and the raise waits until after the commit that would roll it back.
    failure: str | None = None
    epoch = repo_url = branch = sandbox_id = None

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # The lock is held for the whole transaction, which is what
            # serialises concurrent spawns: a second instance blocks here and
            # then reads an epoch that has already moved.
            #
            # Read-then-check rather than a blind UPDATE, so each refusal
            # below can say which one it was.
            try:
                row = sessions._lock_session(cur, session_id)
            except sessions.SessionNotFound:
                raise SpawnRefused(f"no session {session_id}")
            if row["status"] in TERMINAL_STATUSES:
                raise SpawnRefused(
                    f"session {session_id} is {row['status']} and wants no sandbox"
                )

            previous = row["current_epoch"]
            if expect_epoch is not None and previous != expect_epoch:
                raise SpawnRefused(
                    f"session {session_id} is at epoch {previous}, not {expect_epoch}; "
                    "another instance replaced it first"
                )

            # The ceiling counts sandboxes that were lost, not epochs. An
            # ordinary resume opens an epoch too, so counting epochs would
            # fail a healthy conversation for having had gaps in it.
            cur.execute(
                "SELECT count(*) AS lost FROM sandboxes "
                " WHERE session_id = %s AND status IN ('dead','replaced')",
                (session_id,),
            )
            lost = cur.fetchone()["lost"]

            if lost >= config.MAX_SANDBOX_LOSSES:
                # MAX_TOOL_ATTEMPTS cannot cover this: a sandbox dying before
                # it claims anything never moves that counter.
                failure = (
                    f"gave up after losing {lost} sandboxes; "
                    f"the last one because {reason or 'unknown'}"
                )
                sessions._set_status(cur, session_id, FAILED, failure)
            else:
                cur.execute(
                    "UPDATE sessions SET current_epoch = current_epoch + 1, "
                    "updated_at = now() WHERE id = %s RETURNING current_epoch",
                    (session_id,),
                )
                epoch = cur.fetchone()["current_epoch"]
                repo_url = row["repo_url"]
                branch = row["branch"]

                # Always mark the previous epoch's live row replaced. ensure
                # and spawn_for_pending pass no reason, and without this a
                # ready row that registers in the window between ensure's
                # liveness read and this lock stays 'ready' at a stale epoch
                # forever: heartbeats 409, no nudge matches it.
                # 'exited' is kept: that sandbox left rather than being
                # replaced, and overwriting it would read as a death.
                cur.execute(
                    "UPDATE sandboxes SET status = 'replaced' "
                    "WHERE session_id = %s AND epoch = %s AND status <> 'exited'",
                    (session_id, previous),
                )
                if reason is not None:
                    sessions._emit(
                        cur,
                        session_id,
                        "sandbox_died",
                        {"epoch": previous, "reason": reason, "replaced_by": epoch},
                    )

                # Same transaction as the bump, so the replacement finds its
                # work already waiting. The old sandbox cannot claim it back:
                # its epoch is behind. A no-op on a first spawn.
                rescued = sessions.rescue_orphaned_calls(cur, session_id)

                cur.execute(
                    "INSERT INTO sandboxes (session_id, epoch, status) "
                    "VALUES (%s, %s, 'spawning') RETURNING id",
                    (session_id, epoch),
                )
                sandbox_id = cur.fetchone()["id"]

                sessions._emit(cur, session_id, "sandbox_spawning", {"epoch": epoch})

                if rescued:
                    logger.info(
                        "session %s epoch %s inherits %s in-flight call(s): %s",
                        session_id,
                        epoch,
                        len(rescued),
                        ", ".join(c["name"] for c in rescued),
                    )

    if failure:
        raise SpawnRefused(failure)

    # TODO: a third runtime for a remote data plane — an HTTP call to a sandbox
    # service that returns a handle. Same signature as the two below.
    #
    # A failed start leaves the row at 'spawning', which
    # SandboxNudge.reap_stuck_spawning replaces, bounded by MAX_SANDBOX_LOSSES.
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


@log_context.correlated
def ensure(session_id: str) -> dict | None:
    """Guarantee the session has a sandbox. Returns a new one, or None.

    The invariant anything about to give a session work depends on: tool calls
    are only ever run by a sandbox, so a session without one has no way to
    execute what a turn produces.

    Called before advance rather than inside it, so the container boots while
    the model thinks, and so a caller that already knows it has a sandbox can
    skip the question.

    No reason is passed to spawn, because nothing here died. A session arrives
    with no sandbox because the last one left when the session went idle,
    which is an ordinary end rather than a loss.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT e.current_epoch, "
            "       EXISTS (SELECT 1 FROM sandboxes s "
            "                WHERE s.session_id = e.id "
            "                  AND s.epoch = e.current_epoch "
            "                  AND s.status = ANY(%s)) AS live "
            "  FROM sessions e WHERE e.id = %s",
            (list(LIVE_STATUSES), session_id),
        ).fetchone()

    if row is None:
        raise SpawnRefused(f"no session {session_id}")
    if row["live"]:
        return None

    # expect_epoch makes this exactly-once against every other caller doing
    # the same thing, the same way replacement is.
    logger.info("session %s has no sandbox; spawning one", session_id)
    return spawn(session_id, expect_epoch=row["current_epoch"])


@log_context.correlated
def register(session_id: str, epoch: int, container_id: str | None = None) -> dict:
    """A sandbox announces itself. Returns what it needs to clone and check out.

    Once per epoch, from the container spawn() started or a cursord run by
    hand. Re-registering is harmless: it lands on the same row.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            session = sessions._require_current(cur, session_id, epoch)

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
        # Null on a first spawn: the control plane runs no git, so the base is
        # whatever the sandbox finds and reports with its first result. A
        # rebuild reads it back so its diff starts where the work did.
        "base_sha": session["base_sha"],
        # The last SHA the control plane accepted, not whatever the dead
        # sandbox managed to push on its way out.
        "resume_sha": session["last_accepted_sha"],
        "epoch": epoch,
    }


@log_context.correlated
def heartbeat(
    session_id: str,
    epoch: int,
    exiting: bool = False,
    reason: str | None = None,
) -> dict:
    """Liveness, and the one place a sandbox announces its own shutdown.

    A stale epoch raises, which is what tells a zombie to stop. It is the only
    failure a sandbox should die on, so nothing else here may raise a 409.

    Ordinary beats write no event: the row timestamp is the whole record. An
    exit does, because it happens once and says why the sandbox stopped.
    """
    if exiting:
        status = "exited" if reason in CLEAN_EXITS else "dead"
    else:
        status = "ready"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Locks the session, then the sandbox row. spawn holds the
            # same order; a beat that updated sandboxes first could
            # deadlock a replacement of this epoch.
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
                # 'exited' is final: a beat still in flight when the container
                # said goodbye must not put it back in the reaper's index.
                #
                # 'dead' is not final: a beat proves the reaper wrong, and the
                # epoch check above means no replacement has taken over.
                (status, session_id, epoch),
            )
            row = cur.fetchone()
            if row is None:
                # Not a 409: on this endpoint that means "you are stale, stop
                # working", which is the wrong instruction here.
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

    known_hosts is mounted in every mode: without it StrictHostKeyChecking
    refuses the connection, and disabling checking would accept any host key.
    """
    if config.SSH_MODE == "keys":
        # The whole directory. The private key becomes readable by every
        # command the model runs.
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
        # Said out loud because the failure surfaces as a clone that looks
        # like a permissions problem rather than a missing file.
        logger.warning(
            "no known_hosts at %s; the container cannot verify the git remote. "
            "Run: ssh-keyscan github.com >> %s",
            known_hosts,
            known_hosts,
        )

    host_sock = _host_ssh_sock()
    if host_sock is None:
        # Not fatal: a public remote may still clone, and refusing the spawn
        # would hide the real error.
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

    On Docker Desktop the host's SSH_AUTH_SOCK is unreachable from the VM, so
    the fixed published path is used. Either way it only carries keys that
    `ssh-add` has loaded.
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

    Same contract for every runtime: pass cursord its five environment values,
    return a handle for the sandboxes row, and return None rather than raise
    if it could not start.
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

    Shared by both runtimes so neither can drift from the other.
    """
    return {
        "SESSION_ID": session_id,
        "EPOCH": str(epoch),
        "CONTROL_URL": control_url,
        "REPO_URL": repo_url,
        "BRANCH": branch,
    }


# Popen handles for sandboxes this instance started, so exited ones are waited
# on rather than left as zombies. Housekeeping only: the sandboxes row holds a
# pid, so any instance can signal any sandbox without this.
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

    Not a sandbox: run_command executes as this user, on this filesystem, with
    this user's ssh keys and access to the control plane's own database. For
    driving the loop end-to-end without Docker, under supervision.
    """
    _reap_exited()

    if not (config.AGENT_DIR / "cursord").is_dir():
        logger.error(
            "no cursord package under %s; set SANDBOX_AGENT_DIR", config.AGENT_DIR
        )
        return None

    # Per epoch, cleared first: prepare() clones here and git refuses a
    # non-empty directory.
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
    # Without BatchMode a missing key or unknown host prompts on a terminal
    # nobody is attached to, and the clone hangs instead of failing.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

    config.LOG_ROOT.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_ROOT / "{}.{}.log".format(session_id, epoch)

    command = [
        "/bin/bash",
        "-c",
        # $$ is the shell's pid and exec replaces the shell, so cursord runs as
        # the pid recorded below and reports the same handle from register().
        # Otherwise it would report the machine hostname, which nothing can
        # signal.
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
                # Its own session, so a control plane restart does not take the
                # sandbox with it.
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

    None is not fatal: the sandbox row exists at this epoch, so a cursord
    started by hand can register against it.
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

    # Credentials to clone the remote, and to push every checkpoint back.
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
