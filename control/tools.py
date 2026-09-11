"""The tools the model is allowed to call.

No git tools: commits and branches belong to cursord, and model-authored
commits would collide with checkpointing.

TOOL_SCHEMAS is the single source of truth. It is sent to the provider as the
tools array, and render() turns it into prose for the system prompt.
"""

from __future__ import annotations

import json

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 file, relative to the repository root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a file relative to the repository root, creating parent "
                "directories as needed. Supply the complete contents: this is a "
                "full overwrite, not a patch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "contents": {"type": "string"},
                },
                "required": ["path", "contents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files under a directory, relative to the repository root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command from the repository root. Each call gets a "
                "fresh shell, so a cd or an exported variable does not survive "
                "into the next call. Make every command self-contained."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 120},
                },
                "required": ["command"],
            },
        },
    },
]

TOOL_NAMES = frozenset(schema["function"]["name"] for schema in TOOL_SCHEMAS)

# Tools whose re-execution is disclosed to the model. Only run_command can
# leave anything behind: reads are pure, and a repeated write_file overwrites
# with full contents onto a workspace rebuilt from the last accepted commit.
AUDIT_ON_REPEAT = frozenset({"run_command"})


def _signature(function: dict) -> str:
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = set(parameters.get("required") or [])

    parts = []
    for name, spec in properties.items():
        part = "{}: {}".format(name, spec.get("type", "any"))
        if name not in required:
            if "default" in spec:
                part += " = {}".format(json.dumps(spec["default"]))
            else:
                part += " (optional)"
        parts.append(part)
    return ", ".join(parts)


def render() -> str:
    """The tool list as prose, for appending to the system prompt."""
    lines = ["Tools available to you:", ""]
    for schema in TOOL_SCHEMAS:
        function = schema["function"]
        lines.append("  {}({})".format(function["name"], _signature(function)))
        lines.append("      {}".format(function["description"]))
        lines.append("")
    return "\n".join(lines).rstrip()
