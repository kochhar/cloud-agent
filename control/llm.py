"""The model call, behind a seam.

advance() needs exactly one thing from the provider: given the context array,
return text, reasoning, and the tool calls to run next. Keeping that behind
complete() lets tests drive the loop with a scripted model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import tools

_BASE_PROMPT = """You are a coding agent working inside a sandboxed clone of a git repository.

Use the tools below to inspect and modify the repository. Constraints:
- Every run_command call starts a fresh shell at the repository root. A cd or
  an exported variable does not carry over to the next call.
- You have no git tools. Commits and branches are handled for you.
- Write files with their full contents, never a partial patch.

When the task is done, reply with a summary and no tool calls.
"""

# Rendered from tools.TOOL_SCHEMAS rather than written out again, so the prompt
# cannot drift from the schemas the provider is sent.
SYSTEM_PROMPT = _BASE_PROMPT.rstrip() + "\n\n" + tools.render()


class NotConfigured(RuntimeError):
    """No provider client installed."""


@dataclass(frozen=True)
class ToolCall:
    """A tool call the model wants to run, before it becomes a row."""

    name: str
    args: dict
    provider_call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class Reply:
    text: Optional[str] = None
    reasoning: Optional[str] = None
    tool_calls: Sequence[ToolCall] = ()

    @property
    def raw_tool_calls(self) -> Optional[list]:
        """The provider-shaped array, stored so the context replays verbatim."""
        if not self.tool_calls:
            return None
        return [
            {
                "id": call.provider_call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.args},
            }
            for call in self.tool_calls
        ]


# Given the context array, return a Reply.
Client = Callable[[list], Reply]

_client: Optional[Client] = None


def use(client: Optional[Client]) -> None:
    """Install a client. Pass None to remove it."""
    global _client
    _client = client


def complete(context: list) -> Reply:
    # TODO: default implementation calling Grok over HTTP. Until then the only
    # way to run the loop is to install a client with use().
    if _client is None:
        raise NotConfigured("no LLM client installed; call llm.use(...)")
    return _client(context)
