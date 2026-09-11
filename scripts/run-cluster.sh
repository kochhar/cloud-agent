#!/usr/bin/env bash
#
# Run three control-plane instances, one operations dashboard, and Nginx.
# Nginx is the only process exposed publicly, at 0.0.0.0:80.

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

APP_PIDS=()
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

for index in 0 1 2; do
    instance=$((index + 1))
    port="${CONTROL_PORTS[$index]}"
    name="control-$instance"

    (
        cd "$ROOT/control"
        exec env \
            CONTROL_INSTANCE_ID="$name" \
            PYTHONUNBUFFERED=1 \
            "$PYTHON" -m uvicorn app:app \
                --host "$CONTROL_HOST" \
                --port "$port" \
                --root-path /api \
                --proxy-headers \
                --forwarded-allow-ips 127.0.0.1
    ) >>"$LOG_DIR/$name.log" 2>&1 &
    APP_PIDS+=("$!")
done

(
    cd "$ROOT"
    exec env \
        DASHBOARD_INSTANCE_ID=dashboard-1 \
        PYTHONUNBUFFERED=1 \
        "$PYTHON" -m uvicorn dashboard.app:app \
            --host "$DASHBOARD_HOST" \
            --port "$DASHBOARD_PORT" \
            --root-path /ops/dash \
            --proxy-headers \
            --forwarded-allow-ips 127.0.0.1
) >>"$LOG_DIR/dashboard-1.log" 2>&1 &
APP_PIDS+=("$!")

for index in 0 1 2; do
    instance=$((index + 1))
    wait_for_health \
        "control-$instance" \
        "http://$CONTROL_HOST:${CONTROL_PORTS[$index]}/healthz" \
        "${APP_PIDS[$index]}"
done
wait_for_health \
    dashboard-1 \
    "http://$DASHBOARD_HOST:$DASHBOARD_PORT/healthz" \
    "${APP_PIDS[3]}"

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

# Stop the cluster if any child exits instead of silently running at reduced
# capacity. Bash 3 (the macOS system Bash) has no `wait -n`.
while true; do
    for pid in "${APP_PIDS[@]}" "$PROXY_PID"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "A cluster process exited; stopping the remaining processes." >&2
            exit 1
        fi
    done
    sleep 1
done
