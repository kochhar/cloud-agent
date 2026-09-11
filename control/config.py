"""Process configuration, read from the environment once at import.

The server is started as a plain `uvicorn app:app`, with no shell step that
sources .env, so the file is read here instead. Real environment variables
always win: .env is the development default, not an override.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# .env sits at the repository root, one level above this package.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()

        # Quotes are a shell artifact and are not part of the value.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        os.environ.setdefault(name, value)


_load_env_file(ENV_FILE)


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/project1"
)

# xAI speaks the OpenAI chat-completions dialect, so the path is the usual one.
GROK_BASE_URL = os.environ.get("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_API_KEY: Optional[str] = os.environ.get("GROK_API_KEY") or None
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4.6")

# A single turn can spend minutes reasoning before the first byte comes back,
# and advance() holds no lock while it waits, so this is generous on purpose.
GROK_TIMEOUT_SECONDS = float(os.environ.get("GROK_TIMEOUT", "300"))
GROK_MAX_ATTEMPTS = int(os.environ.get("GROK_MAX_ATTEMPTS", "4"))
