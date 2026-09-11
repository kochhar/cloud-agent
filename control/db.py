from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

# dict_row for every connection, so callers get mappings rather than tuples.
pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,
    max_size=20,
    kwargs={"row_factory": dict_row},
    open=True,
)
