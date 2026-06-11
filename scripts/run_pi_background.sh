#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
PID_DIR="$ROOT_DIR/run"

mkdir -p "$LOG_DIR" "$PID_DIR"

stop_if_running() {
  local name="$1"
  local pidfile="$PID_DIR/${name}.pid"

  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      echo "Stopping $name (PID $pid)"
      kill "$pid" 2>/dev/null || true
      for _ in {1..10}; do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 1
      done
      if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing $name (PID $pid)"
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$pidfile"
  fi
}

stop_if_running "pi_exporter"
stop_if_running "pi_mini_server"

start_process() {
  local name="$1"
  shift
  local logfile="$LOG_DIR/${name}.log"
  local pidfile="$PID_DIR/${name}.pid"

  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "$name is already running with PID $(cat "$pidfile")"
    return 0
  fi

  (
    cd "$ROOT_DIR"
    nohup "$@" >>"$logfile" 2>&1 &
    echo $! >"$pidfile"
  )

  echo "Started $name with PID $(cat "$pidfile")"
  echo "Log: $logfile"
}

start_process "pi_mini_server" uv run python3 -m uvicorn pi_mini_server:app --host 0.0.0.0 --port 6000
start_process "pi_exporter" uv run python3 scripts/pi_exporter.py
