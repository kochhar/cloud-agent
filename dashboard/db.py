"""A small, read-only pool isolated from the control plane."""

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=config.POOL_MIN_SIZE,
    max_size=config.POOL_MAX_SIZE,
    kwargs={
        "row_factory": dict_row,
        "options": (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={config.STATEMENT_TIMEOUT_MS}"
        ),
    },
    open=False,
)
