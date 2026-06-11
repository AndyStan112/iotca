#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_DIR="$ROOT_DIR/run"

stop_process() {
  local name="$1"
  local pidfile="$PID_DIR/${name}.pid"

  if [[ ! -f "$pidfile" ]]; then
    echo "$name: no pid file found"
    return 0
  fi

  local pid
  pid="$(cat "$pidfile")"
  if [[ -z "$pid" ]]; then
    echo "$name: empty pid file"
    rm -f "$pidfile"
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
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
  else
    echo "$name is not running"
  fi

  rm -f "$pidfile"
}

stop_process "pi_exporter"
stop_process "pi_mini_server"

