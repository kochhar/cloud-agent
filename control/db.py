from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import config

# dict_row for every connection, so callers get mappings rather than tuples.
pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=2,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=True,
)


def health() -> dict:
    """Prove the pool can hand out a connection that can run a query."""
    with pool.connection() as conn:
        row = conn.execute("SELECT version(), current_database()").fetchone()
    return {"database": row["current_database"], "version": row["version"]}
