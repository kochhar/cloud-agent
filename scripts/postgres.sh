#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/.venv/lib/python3.9/site-packages/pgserver/pginstall/bin"
PGDATA="$ROOT/.pgdata"
LOG="$PGDATA/logfile"
DB_NAME="${PGDATABASE:-project1}"
USER_NAME="${PGUSER:-postgres}"
HOST="${PGHOST:-127.0.0.1}"
PORT="${PGPORT:-5432}"

export PATH="$BIN:$PATH"

usage() {
  echo "Usage: $0 {start|stop|status|psql}"
  exit 1
}

init_cluster() {
  if [[ ! -f "$PGDATA/PG_VERSION" ]]; then
    mkdir -p "$PGDATA"
    "$BIN/initdb" -D "$PGDATA" --auth=trust --auth-local=trust --encoding=UTF8 -U "$USER_NAME" --locale=C
    cat >> "$PGDATA/postgresql.conf" <<EOF

# project1 local FastAPI access
listen_addresses = '127.0.0.1'
port = $PORT
unix_socket_directories = '$PGDATA'
EOF
  fi
}

start() {
  init_cluster
  if "$BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
    echo "Postgres is already running"
  else
    "$BIN/pg_ctl" -D "$PGDATA" -l "$LOG" -w start
  fi
  if ! "$BIN/psql" -h "$HOST" -p "$PORT" -U "$USER_NAME" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    "$BIN/createdb" -h "$HOST" -p "$PORT" -U "$USER_NAME" "$DB_NAME"
  fi
  echo "Postgres ready at postgresql://$USER_NAME@$HOST:$PORT/$DB_NAME"
}

stop() {
  "$BIN/pg_ctl" -D "$PGDATA" -w stop || true
}

status() {
  "$BIN/pg_ctl" -D "$PGDATA" status
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  psql) shift; exec "$BIN/psql" -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB_NAME" "$@" ;;
  *) usage ;;
esac
