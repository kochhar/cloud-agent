"""Row objects for the control plane tables.

Each class maps onto one table in db/schema.sql, so the field names have to
keep matching the column names.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Dict, Optional

# sessions.status, mirroring the CHECK constraint on the table
IDLE = "idle"                    # no sandbox; the turn ended, waiting on the user
THINKING = "thinking"            # an instance is inside the LLM call
EXECUTING = "executing"          # tool calls outstanding
FAILED = "failed"
CANCELLED = "cancelled"

# Takes no more input, ever. 'idle' is deliberately not here: a turn that ends
# without tool calls is finished, not closed, and the next user message picks
# the same session up again.
TERMINAL_STATUSES = frozenset({FAILED, CANCELLED})


@dataclass(frozen=True)
class Session:
    """A snapshot of one sessions row."""

    id: uuid.UUID
    repo_url: str
    branch: str
    base_sha: Optional[str]
    last_accepted_sha: Optional[str]
    status: str
    current_epoch: int
    message_seq: int
    event_seq: int
    thinking_since: Optional[datetime]
    error: Optional[str]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Session":
        """Build from a dict_row, ignoring columns the model does not declare."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in known})

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
