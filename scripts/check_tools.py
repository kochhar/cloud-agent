#!/usr/bin/env python
"""Cross-check the tool schemas against the code that executes them.

`control/tools.py` tells the model what it may call. `agent/cursord/tools.py`
is what actually runs. They ship in different images and cannot import each
other, so nothing but this script stops them from drifting, and drift here is
expensive: a misspelled argument is invisible until a live session calls that
tool, and then it fails on every attempt until the retry ceiling kills the
session.

    .venv/bin/python scripts/check_tools.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control"))
sys.path.insert(0, str(ROOT / "agent"))

import tools as control_tools  # noqa: E402
from cursord import tools as agent_tools  # noqa: E402


def check() -> list[str]:
    problems: list[str] = []

    schemas = {s["function"]["name"]: s["function"] for s in control_tools.TOOL_SCHEMAS}
    registry = agent_tools.REGISTRY

    for name in sorted(set(schemas) - set(registry)):
        problems.append(f"{name}: offered to the model, but cursord cannot execute it")

    for name in sorted(set(registry) - set(schemas)):
        problems.append(f"{name}: cursord implements it, but the model is never told")

    for name in sorted(set(schemas) & set(registry)):
        parameters = schemas[name].get("parameters") or {}
        properties = parameters.get("properties") or {}
        declared_required = set(parameters.get("required") or ())
        declared_optional = set(properties) - declared_required

        tool = registry[name]
        handled_required = set(tool.required)
        handled_optional = set(tool.optional)

        # The spelling check. This is the one that has already bitten us.
        for arg in sorted(declared_required - handled_required):
            if arg in handled_optional:
                problems.append(f"{name}.{arg}: required by the schema, optional in cursord")
            else:
                problems.append(f"{name}.{arg}: in the schema, not read by cursord")

        for arg in sorted(declared_optional - handled_optional - handled_required):
            problems.append(f"{name}.{arg}: offered to the model, ignored by cursord")

        for arg in sorted((handled_required | handled_optional) - set(properties)):
            problems.append(f"{name}.{arg}: cursord expects it, the model is never told about it")

    return problems


def main() -> int:
    problems = check()
    names = sorted(agent_tools.REGISTRY)
    if problems:
        print("tool contract mismatch:\n")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"tool contract OK: {len(names)} tools, arguments agree ({', '.join(names)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
