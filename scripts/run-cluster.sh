#!/usr/bin/env bash
#
# Run three control-plane instances, one operations dashboard, and Nginx.
# Nginx is the only process exposed publicly, at 0.0.0.0:80.
#
# Application processes that exit are restarted in place. Nginx dying still
# stops the cluster: there is nothing to proxy to without it. A slot that
# fails to become healthy MAX_CONSECUTIVE_FAILURES times in a row is treated
# as a crash loop and takes the cluster down with it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-$ROOT/.run/cloud-agent}"
LOG_DIR="$RUN_DIR/logs"
NGINX_CONFIG="${NGINX_CONFIG:-$ROOT/scripts/nginx.conf}"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
NGINX_BIN="${NGINX:-$(command -v nginx || true)}"

CONTROL_HOST="${CONTROL_HOST:-127.0.0.1}"
CONTROL_PORTS=(8101 8102 8103)
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8200}"
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-5}"

# A Docker container cannot reach the host through 127.0.0.1. Both URLs end
# in /api because cursord appends /sandbox/... to this base URL.
export SANDBOX_CONTROL_URL="${SANDBOX_CONTROL_URL:-http://host.docker.internal/api}"
export SANDBOX_LOCAL_CONTROL_URL="${SANDBOX_LOCAL_CONTROL_URL:-http://127.0.0.1/api}"

if [[ ! -x "$PYTHON" ]]; then
    echo "Python environment not found at $PYTHON" >&2
    echo "Create .venv and install control and dashboard requirements first." >&2
    exit 1
fi

if [[ -z "$NGINX_BIN" || ! -x "$NGINX_BIN" ]]; then
    echo "Nginx was not found. Install it or set NGINX=/path/to/nginx." >&2
    echo "On macOS with Homebrew: brew install nginx" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

APP_NAMES=(control-1 control-2 control-3 dashboard-1)
APP_ROLES=(control control control dashboard)
APP_HOSTS=("$CONTROL_HOST" "$CONTROL_HOST" "$CONTROL_HOST" "$DASHBOARD_HOST")
APP_PORTS=("${CONTROL_PORTS[@]}" "$DASHBOARD_PORT")
APP_PIDS=()
APP_FAILURES=()
PROXY_PID=""
PROXY_STARTED=0
PRIVILEGE=()

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    set +e

    if [[ "$PROXY_STARTED" -eq 1 ]]; then
        "${PRIVILEGE[@]}" "$NGINX_BIN" \
            -p "$RUN_DIR/" -c "$NGINX_CONFIG" -s quit >/dev/null 2>&1
    fi

    if [[ "${#APP_PIDS[@]}" -gt 0 ]]; then
        kill "${APP_PIDS[@]}" >/dev/null 2>&1
        wait "${APP_PIDS[@]}" >/dev/null 2>&1
    fi

    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT TERM

wait_for_health() {
    local name=$1
    local url=$2
    local pid=$3
    local attempt

    for attempt in $(seq 1 60); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "$name exited during startup; inspect $LOG_DIR/$name.log" >&2
            return 1
        fi
        if curl --fail --silent --max-time 1 "$url" >/dev/null; then
            return 0
        fi
        sleep 0.5
    done

    echo "$name did not become healthy at $url; inspect $LOG_DIR/$name.log" >&2
    return 1
}

health_url() {
    local index=$1
    echo "http://${APP_HOSTS[$index]}:${APP_PORTS[$index]}/healthz"
}

start_app() {
    local index=$1
    local name="${APP_NAMES[$index]}"
    local host="${APP_HOSTS[$index]}"
    local port="${APP_PORTS[$index]}"
    local role="${APP_ROLES[$index]}"

    if [[ "$role" == "control" ]]; then
        (
            cd "$ROOT/control"
            exec env \
                CONTROL_INSTANCE_ID="$name" \
                PYTHONUNBUFFERED=1 \
                "$PYTHON" -m uvicorn app:app \
                    --host "$host" \
                    --port "$port" \
                    --root-path /api \
                    --proxy-headers \
                    --forwarded-allow-ips 127.0.0.1
        ) >>"$LOG_DIR/$name.log" 2>&1 &
    else
        (
            cd "$ROOT"
            exec env \
                DASHBOARD_INSTANCE_ID="$name" \
                PYTHONUNBUFFERED=1 \
                "$PYTHON" -m uvicorn dashboard.app:app \
                    --host "$host" \
                    --port "$port" \
                    --root-path /ops/dash \
                    --proxy-headers \
                    --forwarded-allow-ips 127.0.0.1
        ) >>"$LOG_DIR/$name.log" 2>&1 &
    fi

    APP_PIDS[$index]=$!
    echo "${APP_PIDS[$index]}" >"$RUN_DIR/$name.pid"
}

restart_app() {
    local index=$1
    local name="${APP_NAMES[$index]}"
    local old="${APP_PIDS[$index]}"

    wait "$old" >/dev/null 2>&1 || true
    echo "$name (pid $old) exited; restarting." >&2

    while true; do
        start_app "$index"
        if wait_for_health "$name" "$(health_url "$index")" "${APP_PIDS[$index]}"; then
            APP_FAILURES[$index]=0
            echo "$name is back as pid ${APP_PIDS[$index]} on ${APP_PORTS[$index]}." >&2
            return 0
        fi
        kill "${APP_PIDS[$index]}" >/dev/null 2>&1 || true
        wait "${APP_PIDS[$index]}" >/dev/null 2>&1 || true
        APP_FAILURES[$index]=$((${APP_FAILURES[$index]} + 1))
        if [[ "${APP_FAILURES[$index]}" -ge "$MAX_CONSECUTIVE_FAILURES" ]]; then
            echo "$name failed to come back after $MAX_CONSECUTIVE_FAILURES attempts; giving up." >&2
            return 1
        fi
        echo "$name restart attempt ${APP_FAILURES[$index]} failed; retrying." >&2
        sleep 1
    done
}

for index in "${!APP_NAMES[@]}"; do
    APP_FAILURES[$index]=0
    start_app "$index"
done

for index in "${!APP_NAMES[@]}"; do
    wait_for_health \
        "${APP_NAMES[$index]}" \
        "$(health_url "$index")" \
        "${APP_PIDS[$index]}"
done

# Binding port 80 requires elevated privilege on macOS and most Linux hosts.
# Only Nginx is elevated; all application processes continue as the caller.
if [[ "$(id -u)" -ne 0 ]]; then
    sudo -v
    PRIVILEGE=(sudo)
fi

"${PRIVILEGE[@]}" "$NGINX_BIN" -t -p "$RUN_DIR/" -c "$NGINX_CONFIG"
"${PRIVILEGE[@]}" "$NGINX_BIN" \
    -p "$RUN_DIR/" \
    -c "$NGINX_CONFIG" \
    -g "daemon off;" &
PROXY_PID=$!
PROXY_STARTED=1

echo "Cloud agent: http://127.0.0.1/ui/"
echo "Control API: http://127.0.0.1/api/"
echo "Operations:  http://127.0.0.1/ops/dash/"
echo "Logs:        $LOG_DIR"
echo "Press Ctrl-C to stop all instances."

# Bash 3 (the macOS system Bash) has no `wait -n`. Poll instead, and restart
# any application process that has exited. Nginx is not restarted: a dead
# proxy is a dead cluster.
while true; do
    if ! kill -0 "$PROXY_PID" 2>/dev/null; then
        echo "Nginx exited; stopping the remaining processes." >&2
        exit 1
    fi
    for index in "${!APP_NAMES[@]}"; do
        if ! kill -0 "${APP_PIDS[$index]}" 2>/dev/null; then
            restart_app "$index"
        fi
    done
    sleep 1
done
