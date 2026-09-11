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
