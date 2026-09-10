"""The clone, and the checkpoint after every tool call.

Git is the only thing in this container that survives it. The model never sees
any of this: git is not in the tool schema, so model-authored commits cannot
collide with checkpointing.

The invariant: when a tool call's result is accepted by the control plane, the
file state that produced it is already in the bare repo under the SHA reported
alongside it. Push happens before report, always. The cost of that ordering is
that a container dying in between leaves a pushed commit nobody accepted,
which is exactly what `reset_to` throws away on the next spawn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


async def git(*args: str, check: bool = True) -> str:
    """Run a git command in the workspace and return stdout."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(config.WORKSPACE) if config.WORKSPACE.exists() else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if check and process.returncode != 0:
        raise GitError(
            "git {} failed ({}): {}".format(
                " ".join(args), process.returncode, err.decode(errors="replace").strip()
            )
        )
    return out.decode(errors="replace")


async def prepare(repo_url: str, branch: str, resume_sha: Optional[str]) -> str:
    """Clone and put the tree exactly where the control plane says it is.

    Identical on a first spawn and on a rebuild, which is the point: recovery
    is not a special path, it is the same path with a different starting SHA.
    Returns the resolved head SHA, which is the base on a first spawn.
    """
    config.WORKSPACE.parent.mkdir(parents=True, exist_ok=True)
    await git("clone", repo_url, str(config.WORKSPACE))

    await git("config", "user.name", config.GIT_AUTHOR_NAME)
    await git("config", "user.email", config.GIT_AUTHOR_EMAIL)

    if await _remote_has(branch):
        # Rebuild: the branch exists because a previous epoch made it.
        await git("checkout", "-B", branch, f"origin/{branch}")
    else:
        # First spawn: a fresh branch off whatever the clone defaulted to.
        await git("checkout", "-b", branch)

    if resume_sha:
        await reset_to(resume_sha)

    return (await git("rev-parse", "HEAD")).strip()


async def reset_to(sha: str) -> None:
    """Force the branch back to the last SHA the control plane accepted.

    This is the reconciliation half of the epoch scheme. A zombie sandbox can
    still push after its replacement has started; its commits are unreachable
    once we reset past them and force-push.
    """
    await git("reset", "--hard", sha)


async def checkpoint(message: str) -> Optional[str]:
    """Commit and push if the tool call changed anything. Returns the SHA.

    None means the tree was clean, which is the common case: reads, listings,
    and most commands touch nothing. No commit, no SHA, nothing to reconcile.
    """
    await git("add", "-A")

    # Ignored files are invisible to `add -A` and so never survive a rebuild.
    # A small allowlist covers the case that actually matters in practice.
    for path in config.FORCE_ADD_PATHS:
        if (config.WORKSPACE / path).exists():
            await git("add", "--force", "--", path, check=False)

    staged = await git("diff", "--cached", "--name-only")
    if not staged.strip():
        return None

    await git("commit", "--quiet", "--message", message)
    sha = (await git("rev-parse", "HEAD")).strip()

    # Force, because we may be sitting behind a zombie's push. The control
    # plane's last_accepted_sha is the authority on the branch, not the tip.
    await git("push", "--force", "origin", f"HEAD:refs/heads/{config.BRANCH}")
    logger.info("checkpoint %s", sha[:12])
    return sha


async def _remote_has(branch: str) -> bool:
    out = await git("ls-remote", "--heads", "origin", branch)
    return bool(out.strip())
