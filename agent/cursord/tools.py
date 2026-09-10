"""The four tools, as they actually execute.

cursord has no idea what any of this means. It receives a name and an args
object, runs it, and reports what happened. The schemas the model sees live in
`control/tools.py`, which is the source of truth for names and argument
spellings; this is only the execution half. The two halves ship in different
images and cannot import each other, so `scripts/check_tools.py` compares them.

Deliberately absent: anything git. Checkpointing owns the repository, and a
model that can `git checkout` can undo a checkpoint out from under it.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import config


@dataclass(frozen=True)
class ToolResult:
    """What goes back to the control plane. `output` lands in the model's context."""

    output: str
    exit_code: Optional[int] = 0


class ToolError(Exception):
    """A tool failed in a way the model should see and can act on."""


# ---------------------------------------------------------------------------
# path safety
# ---------------------------------------------------------------------------


def _resolve(raw: str) -> Path:
    """Resolve a model-supplied path inside the workspace, or refuse.

    Not a security boundary — the container is the boundary, and run_command
    can reach anywhere regardless. This is here so that a mistaken absolute
    path fails with something the model can correct instead of silently
    writing outside the tree, where it would not be committed and would not
    survive a rebuild.
    """
    root = config.WORKSPACE.resolve()
    candidate = (root / raw).resolve() if not os.path.isabs(raw) else Path(raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError(f"path {raw!r} is outside the workspace")
    return candidate


def _truncate(text: str, limit: int = config.MAX_OUTPUT_BYTES) -> str:
    """Keep the head and the tail. The middle is where the noise is."""
    data = text.encode(errors="replace")
    if len(data) <= limit:
        return text
    half = limit // 2
    dropped = len(data) - limit
    return (
        data[:half].decode(errors="replace")
        + f"\n\n... [{dropped} bytes truncated] ...\n\n"
        + data[-half:].decode(errors="replace")
    )


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


async def read_file(args: dict[str, Any]) -> ToolResult:
    path = _resolve(args["path"])
    if not path.is_file():
        raise ToolError(f"{args['path']}: no such file")
    data = path.read_bytes()
    if len(data) > config.MAX_READ_BYTES:
        # Say so, or the model reasons about a file it only partly saw.
        return ToolResult(
            output=data[: config.MAX_READ_BYTES].decode(errors="replace")
            + f"\n\n... [truncated at {config.MAX_READ_BYTES} bytes of {len(data)}]"
        )
    return ToolResult(output=data.decode(errors="replace"))


async def write_file(args: dict[str, Any]) -> ToolResult:
    """Write full contents. Idempotent, which is what makes a re-run harmless."""
    path = _resolve(args["path"])
    contents = args["contents"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return ToolResult(output=f"wrote {len(contents)} bytes to {args['path']}")


async def list_files(args: dict[str, Any]) -> ToolResult:
    path = _resolve(args.get("path", "."))
    if not path.is_dir():
        raise ToolError(f"{args.get('path', '.')}: not a directory")
    root = config.WORKSPACE.resolve()
    entries = []
    for entry in sorted(path.rglob("*") if args.get("recursive") else path.iterdir()):
        if ".git" in entry.parts:
            continue
        entries.append(
            str(entry.relative_to(root)) + ("/" if entry.is_dir() else "")
        )
        if len(entries) >= config.MAX_LIST_ENTRIES:
            entries.append(f"... [truncated at {config.MAX_LIST_ENTRIES} entries]")
            break
    return ToolResult(output="\n".join(entries) or "(empty)")


async def run_command(args: dict[str, Any]) -> ToolResult:
    """Run a shell command from the repo root.

    Each call is its own process group with its own shell. A `cd` or an export
    in one call is not visible in the next; that constraint is declared in the
    tool description so the model works with it rather than around it. Keeping
    it means a tool call has no dependency on the container that ran the
    previous one, which is what makes recovery a rebuild rather than a replay
    of accumulated shell state.
    """
    command = args["command"]
    timeout = float(args.get("timeout_seconds") or config.COMMAND_TIMEOUT_SECONDS)

    process = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        command,
        cwd=str(config.WORKSPACE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # The whole group, or backgrounded children outlive the call and hold
        # the pipe open.
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        await process.wait()
        return ToolResult(
            output=f"command timed out after {timeout:.0f}s and was killed",
            exit_code=124,
        )

    return ToolResult(
        output=_truncate(out.decode(errors="replace")),
        exit_code=process.returncode,
    )


@dataclass(frozen=True)
class Tool:
    """A handler plus the argument names it answers to.

    The names are spelled out rather than inferred because they are half of a
    contract whose other half lives in `control/tools.py`, in a different
    process and a different image. A rename on that side used to surface as a
    tool failing every time the model called it, mid-session. Declared here,
    `scripts/check_tools.py` catches it before anything is built.
    """

    handler: Callable[[dict[str, Any]], Awaitable[ToolResult]]
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


REGISTRY = {
    "read_file": Tool(read_file, required=("path",)),
    "write_file": Tool(write_file, required=("path", "contents")),
    "list_files": Tool(list_files, optional=("path", "recursive")),
    "run_command": Tool(run_command, required=("command",), optional=("timeout_seconds",)),
}


async def execute(name: str, args: dict[str, Any]) -> ToolResult:
    """Run one tool, turning any failure into a result the model can read.

    Nothing here raises. A tool that blew up is a normal turn in the
    conversation, and a sandbox that died because a tool threw would cost a
    whole epoch to say so.
    """
    tool = REGISTRY.get(name)
    if tool is None:
        known = ", ".join(sorted(REGISTRY))
        return ToolResult(output=f"unknown tool {name!r}; expected one of {known}", exit_code=1)

    missing = [key for key in tool.required if args.get(key) is None]
    if missing:
        return ToolResult(
            output="missing required argument(s): {}".format(", ".join(missing)),
            exit_code=1,
        )

    try:
        return await tool.handler(args)
    except ToolError as exc:
        return ToolResult(output=str(exc), exit_code=1)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return ToolResult(output=f"{type(exc).__name__}: {exc}", exit_code=1)
