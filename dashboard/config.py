"""Dashboard-only configuration.

The dashboard intentionally duplicates the one database setting it shares
with control so importing it cannot initialize or couple to a control process.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


_load_env()

DATABASE_URL = os.environ.get(
    "DASHBOARD_DATABASE_URL",
    os.environ.get(
        "DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/project1"
    ),
)
POOL_MIN_SIZE = int(os.environ.get("DASHBOARD_DB_POOL_MIN", "1"))
POOL_MAX_SIZE = int(os.environ.get("DASHBOARD_DB_POOL_MAX", "4"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("DASHBOARD_STATEMENT_TIMEOUT_MS", "3000"))
CACHE_SECONDS = float(os.environ.get("DASHBOARD_CACHE_SECONDS", "10"))

# Same grok-4.6 defaults as control. The dashboard prices stored tokens at
# query time so attempts recorded before prices were configured still cost.
INPUT_COST_PER_MILLION = float(os.environ.get("GROK_INPUT_COST_PER_MILLION", "2"))
OUTPUT_COST_PER_MILLION = float(os.environ.get("GROK_OUTPUT_COST_PER_MILLION", "6"))
LONG_CONTEXT_TOKENS = int(os.environ.get("GROK_LONG_CONTEXT_TOKENS", "200000"))
LONG_CONTEXT_MULTIPLIER = float(os.environ.get("GROK_LONG_CONTEXT_MULTIPLIER", "2"))
