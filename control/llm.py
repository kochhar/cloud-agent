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
import log_context
import telemetry
import tools

logger = logging.getLogger(__name__)

_BASE_PROMPT = """You are a coding agent working inside a sandboxed clone of a git repository.

Use the tools below to inspect and modify the repository. Constraints:
- Every run_command call starts a fresh shell at the repository root. A cd or
  an exported variable does not carry over to the next call.
- You have no git tools. Commits and branches are handled for you.
- Write files with their full contents, never a partial patch.

Your sandbox can die mid-task and be replaced. When that happens a tool result
arrives with a [repeat] marker, meaning the call was dispatched again and may
have executed more than once. You are the one who decides what to do about it:
- Your workspace is rebuilt from the last commit that was accepted, so file
  edits made by the lost attempt are already undone. Do not re-apply them and
  do not clean them up.
- What does not roll back is anything a command sent outside the workspace: a
  pull request or issue comment, a published package, a deploy or a triggered
  workflow, a write to a shared database, a webhook, a message. Those can
  land twice.
- So on a [repeat] of a command that did one of those, look at the current
  state before doing it again, and prefer a command that checks and then acts
  over one that blindly repeats. On a [repeat] of anything else — a build, a
  test run, a search, a read — just carry on.

When the task is done, reply with a summary and no tool calls.
"""

# Rendered from TOOL_SCHEMAS so the prompt cannot drift from what is sent.
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

    # The provider's own array, kept byte-for-byte. None for scripted clients.
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
                # A JSON string, not an object: replaying it in any other
                # shape is a different request.
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


def complete(
    context: list,
    session_id: Optional[str] = None,
    on_attempt: Optional[Callable[[], None]] = None,
) -> Reply:
    """One model turn. An installed client wins, so tests never hit the network.

    `session_id` only labels log lines, and is not passed to an installed
    client, so the Client signature stays one argument.

    `on_attempt` runs at the start of each HTTP try and again when a try
    fails before the backoff sleep, so the caller can keep a liveness clock
    without this module knowing about sessions. Installed clients do not
    retry and do not call it.
    """
    if _client is not None:
        return _client(context)
    with log_context.bind(session_id=session_id):
        return _grok(context, session_id, on_attempt=on_attempt)


# ---------- ---------- ----------
# Grok
# ---------- ---------- ----------

# Safe to retry: nothing has been written yet, so the turn is idempotent.
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


def _grok(
    context: list,
    session_id: Optional[str] = None,
    on_attempt: Optional[Callable[[], None]] = None,
) -> Reply:
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
    call_id = str(uuid.uuid4())
    context_chars = _context_chars(context)

    last: Optional[Exception] = None
    for attempt in range(1, config.GROK_MAX_ATTEMPTS + 1):
        if on_attempt is not None:
            on_attempt()
        telemetry_id = (
            telemetry.start_llm_attempt(
                session_id=session_id,
                call_id=call_id,
                attempt=attempt,
                provider="xai",
                model=config.GROK_MODEL,
                context_chars=context_chars,
                message_count=len(context),
            )
            if session_id is not None
            else None
        )
        logger.info(
            "grok request %s attempt %s/%s: %s messages, ~%s chars, %s tools",
            config.GROK_MODEL,
            attempt,
            config.GROK_MAX_ATTEMPTS,
            len(context),
            context_chars,
            len(tools.TOOL_SCHEMAS),
            extra={
                "event": "llm_request",
                "attempt": attempt,
                "model": config.GROK_MODEL,
                "message_count": len(context),
                "context_chars": context_chars,
                "tool_count": len(tools.TOOL_SCHEMAS),
            },
        )

        started = time.monotonic()
        try:
            response = _session().post(
                "/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            elapsed = time.monotonic() - started
            category = (
                "timeout" if isinstance(exc, httpx.TimeoutException) else "transport"
            )
            last = ProviderError("could not reach the provider: {}".format(exc))
            telemetry.finish_llm_attempt(
                telemetry_id,
                outcome=category,
                is_final=attempt == config.GROK_MAX_ATTEMPTS,
                duration_ms=round(elapsed * 1000, 3),
            )
            logger.warning(
                "grok unreachable after %.1fs: %s",
                elapsed,
                exc,
                extra={
                    "event": "llm_error",
                    "attempt": attempt,
                    "duration_ms": round(elapsed * 1000, 3),
                    "error_category": category,
                    "model": config.GROK_MODEL,
                },
            )
        else:
            elapsed = time.monotonic() - started

            if response.status_code == 200:
                try:
                    payload = response.json()
                    reply = _parse(payload)
                except (ProviderError, ValueError, TypeError, KeyError) as exc:
                    last = ProviderError(
                        "provider returned a malformed response: {}".format(exc)
                    )
                    telemetry.finish_llm_attempt(
                        telemetry_id,
                        outcome="malformed_response",
                        is_final=True,
                        duration_ms=round(elapsed * 1000, 3),
                        http_status=response.status_code,
                    )
                    logger.warning(
                        "grok returned malformed JSON after %.1fs: %s",
                        elapsed,
                        exc,
                        extra={
                            "event": "llm_error",
                            "attempt": attempt,
                            "duration_ms": round(elapsed * 1000, 3),
                            "status_code": response.status_code,
                            "error_category": "malformed_response",
                            "model": config.GROK_MODEL,
                        },
                    )
                    break
                usage = payload.get("usage") or {}
                telemetry.finish_llm_attempt(
                    telemetry_id,
                    outcome="success",
                    is_final=True,
                    duration_ms=round(elapsed * 1000, 3),
                    http_status=response.status_code,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    tool_call_count=len(reply.tool_calls),
                )
                # Elapsed matters: a turn outlasting the sandbox's read
                # timeout is what makes cursord retry an accepted result.
                logger.info(
                    "grok response %s in %.1fs: %s",
                    config.GROK_MODEL,
                    elapsed,
                    _describe(payload, reply),
                    extra={
                        "event": "llm_response",
                        "attempt": attempt,
                        "duration_ms": round(elapsed * 1000, 3),
                        "status_code": response.status_code,
                        "model": config.GROK_MODEL,
                        "tokens_in": usage.get("prompt_tokens"),
                        "tokens_out": usage.get("completion_tokens"),
                        "tool_count": len(reply.tool_calls),
                        "finish_reason": (
                            (payload.get("choices") or [{}])[0].get("finish_reason")
                        ),
                    },
                )
                return reply

            # Safe to log: the key is in the request headers, not the body.
            detail = response.text.strip()[:500]
            last = ProviderError(
                "provider returned {}: {}".format(response.status_code, detail)
            )
            category = _http_error_category(response.status_code)
            retryable = response.status_code in _RETRY_STATUSES
            telemetry.finish_llm_attempt(
                telemetry_id,
                outcome=category,
                is_final=not retryable or attempt == config.GROK_MAX_ATTEMPTS,
                duration_ms=round(elapsed * 1000, 3),
                http_status=response.status_code,
            )
            logger.warning(
                "grok response %s in %.1fs: %s",
                response.status_code,
                elapsed,
                detail,
                extra={
                    "event": "llm_error",
                    "attempt": attempt,
                    "duration_ms": round(elapsed * 1000, 3),
                    "status_code": response.status_code,
                    "error_category": category,
                    "model": config.GROK_MODEL,
                },
            )
            if not retryable:
                break

        if attempt < config.GROK_MAX_ATTEMPTS:
            # The try is over. Refresh before sleeping so a full-timeout
            # attempt plus backoff cannot look like a dead caller.
            if on_attempt is not None:
                on_attempt()
            delay = _backoff(attempt)
            logger.warning(
                "grok attempt %s/%s failed (%s); retrying in %.1fs",
                attempt,
                config.GROK_MAX_ATTEMPTS,
                last,
                delay,
                extra={
                    "event": "llm_retry",
                    "attempt": attempt,
                    "error_category": "retry",
                    "model": config.GROK_MODEL,
                },
            )
            time.sleep(delay)

    raise last if last else ProviderError("no attempt was made")


def _context_chars(context: list) -> int:
    """Roughly how much is going up. Sizes only, never the content itself."""
    return sum(len(message.get("content") or "") for message in context)


def _http_error_category(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit"
    if status_code == 408:
        return "timeout"
    if status_code >= 500:
        return "provider_5xx"
    return "provider_4xx"


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

    # A refusal arrives instead of content, and reads as an ordinary final
    # answer: no tool calls, so the turn ends showing why.
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
            # Dispatched anyway: the sandbox reports a missing-argument error,
            # which is the feedback the model needs to retry.
            logger.warning(
                "unparseable arguments for tool %s: %r",
                name,
                arguments,
                extra={
                    "event": "llm_malformed_tool_call",
                    "error_category": "malformed_tool_call",
                },
            )
            args = {}

    if name not in tools.TOOL_NAMES:
        # Also dispatched: the sandbox rejects it and tells the model, rather
        # than the loop dying on a hallucinated name.
        logger.warning(
            "model asked for unknown tool %r",
            name,
            extra={
                "event": "llm_unknown_tool",
                "error_category": "malformed_tool_call",
            },
        )

    return ToolCall(
        name=name,
        args=args,
        # The provider's id, so our tool message matches the call it answers.
        provider_call_id=entry.get("id") or uuid.uuid4().hex,
    )
