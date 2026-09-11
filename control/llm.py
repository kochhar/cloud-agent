"""The model call, behind a seam.

advance() needs exactly one thing from the provider: given the context array,
return text, reasoning, and the tool calls to run next. Keeping that behind
complete() lets tests drive the loop with a scripted model.

The default implementation talks to Grok. xAI speaks the OpenAI
chat-completions dialect, so the context array _load_context() rebuilds goes
onto the wire unchanged.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import httpx

import config
import tools

logger = logging.getLogger(__name__)

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


class ProviderError(RuntimeError):
    """The provider could not be reached, or answered with an error."""


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

    # The provider's own tool_calls array, kept byte-for-byte when the reply
    # came from a provider. Scripted clients leave it None.
    raw: Optional[list] = None

    @property
    def raw_tool_calls(self) -> Optional[list]:
        """The provider-shaped array, stored so the context replays verbatim."""
        if self.raw is not None:
            return self.raw
        if not self.tool_calls:
            return None
        return [
            {
                "id": call.provider_call_id,
                "type": "function",
                # A string, not an object: that is what the API emits, and
                # replaying it in any other shape is a different request.
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.args),
                },
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


def complete(context: list, session_id: Optional[str] = None) -> Reply:
    """One model turn. An installed client wins, so tests never hit the network.

    `session_id` is only ever used to label log lines. It is not passed to an
    installed client, so the Client signature stays one argument and every
    scripted stub keeps working.
    """
    if _client is not None:
        return _client(context)
    return _grok(context, session_id)


# ---------- ---------- ----------
# Grok
# ---------- ---------- ----------

# Retried: the provider is busy or briefly broken, and the turn is idempotent
# because nothing has been written yet.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})

_http: Optional[httpx.Client] = None


def _session() -> httpx.Client:
    """One client for the process, so connections survive between turns."""
    global _http
    if _http is None:
        _http = httpx.Client(
            base_url=config.GROK_BASE_URL,
            timeout=httpx.Timeout(
                config.GROK_TIMEOUT_SECONDS, connect=10.0
            ),
        )
    return _http


def _grok(context: list, session_id: Optional[str] = None) -> Reply:
    if not config.GROK_API_KEY:
        raise NotConfigured(
            "GROK_API_KEY is unset; put it in .env or install a client with llm.use(...)"
        )

    body = {
        "model": config.GROK_MODEL,
        "messages": context,
        "tools": tools.TOOL_SCHEMAS,
        "tool_choice": "auto",
    }
    headers = {"Authorization": "Bearer {}".format(config.GROK_API_KEY)}

    # Turns run in the request threadpool, so several can be in the air at
    # once and their log lines interleave. Every line below carries this, so
    # a call and its answer can be paired back up.
    tag = "[{}]".format(session_id[:8] if session_id else "?")

    last: Optional[Exception] = None
    for attempt in range(1, config.GROK_MAX_ATTEMPTS + 1):
        logger.info(
            "%s grok request  %s attempt %s/%s: %s messages, ~%s chars, %s tools",
            tag,
            config.GROK_MODEL,
            attempt,
            config.GROK_MAX_ATTEMPTS,
            len(context),
            _context_chars(context),
            len(tools.TOOL_SCHEMAS),
        )

        started = time.monotonic()
        try:
            response = _session().post(
                "/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            last = ProviderError("could not reach the provider: {}".format(exc))
            logger.warning(
                "%s grok unreachable after %.1fs: %s", tag, time.monotonic() - started, exc
            )
        else:
            elapsed = time.monotonic() - started

            if response.status_code == 200:
                payload = response.json()
                reply = _parse(payload)
                # The elapsed time is the number that matters here: a turn
                # outlasting the sandbox's read timeout is what makes cursord
                # retry a result the control plane already took.
                logger.info(
                    "%s grok response %s in %.1fs: %s",
                    tag,
                    config.GROK_MODEL,
                    elapsed,
                    _describe(payload, reply),
                )
                return reply

            # The body carries the reason; the key is only in the request
            # headers, so this is safe to log and to surface to the client.
            detail = response.text.strip()[:500]
            last = ProviderError(
                "provider returned {}: {}".format(response.status_code, detail)
            )
            logger.warning(
                "%s grok response %s in %.1fs: %s",
                tag,
                response.status_code,
                elapsed,
                detail,
            )
            if response.status_code not in _RETRY_STATUSES:
                break

        if attempt < config.GROK_MAX_ATTEMPTS:
            delay = _backoff(attempt)
            logger.warning(
                "%s grok attempt %s/%s failed (%s); retrying in %.1fs",
                tag,
                attempt,
                config.GROK_MAX_ATTEMPTS,
                last,
                delay,
            )
            time.sleep(delay)

    raise last if last else ProviderError("no attempt was made")


def _context_chars(context: list) -> int:
    """Roughly how much is going up. Sizes only, never the content itself."""
    return sum(len(message.get("content") or "") for message in context)


def _describe(payload: dict, reply: Reply) -> str:
    """What came back, in sizes and counts rather than text."""
    choice = (payload.get("choices") or [{}])[0]
    parts = ["finish={}".format(choice.get("finish_reason") or "?")]

    if reply.tool_calls:
        parts.append(
            "{} tool calls ({})".format(
                len(reply.tool_calls),
                ", ".join(call.name for call in reply.tool_calls),
            )
        )
    else:
        parts.append("no tool calls")

    if reply.text:
        parts.append("{} chars text".format(len(reply.text)))
    if reply.reasoning:
        parts.append("{} chars reasoning".format(len(reply.reasoning)))

    usage = payload.get("usage") or {}
    if usage:
        parts.append(
            "tokens {}+{}".format(
                usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?")
            )
        )
    return ", ".join(parts)


def _backoff(attempt: int) -> float:
    """Exponential, with jitter so parallel sessions do not retry in lockstep."""
    return min(2.0 ** attempt, 30.0) * (0.5 + random.random() / 2)


def _parse(payload: dict) -> Reply:
    choices = payload.get("choices") or []
    if not choices:
        raise ProviderError("provider returned no choices")

    message = choices[0].get("message") or {}

    # A refusal arrives instead of content, and reads to the loop as an
    # ordinary final answer: no tool calls, so the turn ends and the user sees
    # why rather than an empty bubble.
    text = message.get("content") or message.get("refusal") or None

    # Named reasoning_content on xAI, reasoning elsewhere.
    reasoning = message.get("reasoning_content") or message.get("reasoning") or None

    raw = message.get("tool_calls") or None
    return Reply(
        text=text,
        reasoning=reasoning,
        tool_calls=tuple(_tool_call(entry) for entry in raw or ()),
        raw=raw,
    )


def _tool_call(entry: dict) -> ToolCall:
    function = entry.get("function") or {}
    name = function.get("name") or ""
    arguments = function.get("arguments")

    if isinstance(arguments, dict):
        args: dict[str, Any] = arguments
    else:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            # Not worth failing the session over. Dispatching with no
            # arguments makes the sandbox report a missing-argument error,
            # which is the feedback the model needs to try again.
            logger.warning("unparseable arguments for tool %s: %r", name, arguments)
            args = {}

    if name not in tools.TOOL_NAMES:
        # Still dispatched: the sandbox rejects it and tells the model, which
        # is a better outcome than the loop dying on a hallucinated name.
        logger.warning("model asked for unknown tool %r", name)

    return ToolCall(
        name=name,
        args=args,
        # The provider's id, so the tool message we send back matches the call
        # it is answering.
        provider_call_id=entry.get("id") or uuid.uuid4().hex,
    )
