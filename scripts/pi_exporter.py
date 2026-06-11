#!/usr/bin/env python3
from __future__ import annotations

import base64
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

SERVER_URL = os.getenv("SERVER_URL")
DEVICE_NAME = os.getenv("DEVICE_NAME", "raspberry-pi")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")
LOCAL_PI_URL = os.getenv("LOCAL_PI_URL", "http://127.0.0.1:6000")
PI_TOKEN = os.getenv("PI_TOKEN")
DATA_INTERVAL_SECONDS = int(os.getenv("DATA_INTERVAL_SECONDS", "5"))
COMMAND_INTERVAL_SECONDS = int(os.getenv("COMMAND_INTERVAL_SECONDS", "1"))
TELEMETRY_REQUEST_TIMEOUT = float(os.getenv("TELEMETRY_REQUEST_TIMEOUT", os.getenv("REQUEST_TIMEOUT", "10.0")))
COMMAND_REQUEST_TIMEOUT = float(os.getenv("COMMAND_REQUEST_TIMEOUT", "2.0"))
UPLOAD_CAMERA_SNAPSHOT = os.getenv("UPLOAD_CAMERA_SNAPSHOT", "true").lower() not in {"0", "false", "no"}

if not SERVER_URL:
    print("ERROR: SERVER_URL is required in .env")
    sys.exit(1)

if not DEVICE_TOKEN:
    print("ERROR: DEVICE_TOKEN is required in .env")
    sys.exit(1)

if not PI_TOKEN:
    print("ERROR: PI_TOKEN is required in .env")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {DEVICE_TOKEN}",
    "Content-Type": "application/json",
}

PI_HEADERS = {
    "Authorization": f"Bearer {PI_TOKEN}",
    "Content-Type": "application/json",
}

camera_defaults_cache: dict[str, Any] = {}
camera_defaults_lock = threading.Lock()


def get_cached_camera_defaults() -> dict[str, Any]:
    with camera_defaults_lock:
        return dict(camera_defaults_cache)


def set_cached_camera_defaults(camera_defaults: dict[str, Any] | None):
    with camera_defaults_lock:
        camera_defaults_cache.clear()
        camera_defaults_cache.update(camera_defaults or {})


def fetch_local_status():
    url = LOCAL_PI_URL.rstrip("/") + "/api/status"
    response = requests.get(url, headers=PI_HEADERS, timeout=TELEMETRY_REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data") or {}


def fetch_device_config():
    url = SERVER_URL.rstrip("/") + "/api/device/config"
    response = requests.get(
        url,
        headers=HEADERS,
        params={"device_name": DEVICE_NAME},
        timeout=TELEMETRY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("camera_defaults") or {}


def fetch_local_camera_snapshot(camera_defaults: dict[str, Any] | None = None):
    url = LOCAL_PI_URL.rstrip("/") + "/api/camera"
    response = requests.get(
        url,
        headers=PI_HEADERS,
        params=camera_defaults or {},
        timeout=TELEMETRY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.content


def merge_camera_defaults(base: dict[str, Any] | None, overrides: dict[str, Any] | None):
    merged = dict(base or {})
    for key, value in (overrides or {}).items():
        if value is None or value == "":
            continue
        merged[key] = value
    return merged


def build_telemetry():
    status = fetch_local_status()

    def numeric(value):
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return value

    return {
        "device_name": DEVICE_NAME,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "source": "pi_exporter",
            "pi_url": LOCAL_PI_URL,
        },
        "metrics": [
            {"metric": "temperatura", "value": numeric(status.get("temperatura")), "payload": {}},
            {"metric": "umiditate", "value": numeric(status.get("umiditate")), "payload": {}},
            {"metric": "lumina", "value": numeric(status.get("lumina")), "payload": {}},
            {"metric": "pompa", "value": numeric(status.get("pompa")), "payload": {"state": status.get("pompa")}},
            {"metric": "incalzire", "value": numeric(status.get("incalzire")), "payload": {"state": status.get("incalzire")}},
            {"metric": "racire", "value": numeric(status.get("racire")), "payload": {"state": status.get("racire")}},
        ],
    }


def post_telemetry(data):
    url = SERVER_URL.rstrip("/") + "/api/telemetry"
    response = requests.post(url, headers=HEADERS, json=data, timeout=TELEMETRY_REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def post_camera_snapshot(image_bytes: bytes):
    if not UPLOAD_CAMERA_SNAPSHOT:
        return None
    url = SERVER_URL.rstrip("/") + "/api/camera/snapshot"
    payload = {
        "device_name": DEVICE_NAME,
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
    }
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {DEVICE_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TELEMETRY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def claim_commands(limit: int = 10):
    url = SERVER_URL.rstrip("/") + "/api/commands/claim"
    response = requests.post(
        url,
        headers=HEADERS,
        json={"device_name": DEVICE_NAME, "limit": limit},
        timeout=COMMAND_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("rows") or []


def schedule_due_recurring_jobs(limit: int = 20):
    url = SERVER_URL.rstrip("/") + "/api/recurring-jobs/run-due"
    response = requests.post(
        url,
        headers=HEADERS,
        json={"device_name": DEVICE_NAME, "limit": limit},
        timeout=TELEMETRY_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("scheduled") or []


def execute_local_command(command: str, parameters: dict[str, Any]):
    url = LOCAL_PI_URL.rstrip("/") + "/api/control"
    response = requests.post(
        url,
        headers=PI_HEADERS,
        json={"command": command, "parameters": parameters},
        timeout=COMMAND_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def ack_command(command_id: int, status: str, result: dict[str, Any]):
    url = SERVER_URL.rstrip("/") + "/api/commands/ack"
    response = requests.post(
        url,
        headers=HEADERS,
        json={
            "device_name": DEVICE_NAME,
            "command_id": command_id,
            "status": status,
            "result": result,
        },
        timeout=COMMAND_REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def process_commands(camera_defaults: dict[str, Any] | None = None):
    commands = claim_commands()
    if not commands:
        return

    for command_row in commands:
        command_id = command_row["id"]
        command = command_row["command"]
        parameters = command_row.get("parameters") or {}
        try:
            if command == "camera_capture":
                capture_params = merge_camera_defaults(camera_defaults, parameters)
                snapshot = fetch_local_camera_snapshot(capture_params)
                camera_result = post_camera_snapshot(snapshot)
                result = {
                    "status": "captured",
                    "camera_defaults": capture_params,
                    "camera_result": camera_result,
                }
            else:
                result = execute_local_command(command, parameters)
            ack_command(command_id, "completed", result)
            print(f"Command {command_id} executed:", result)
        except Exception as exc:
            error_result = {"error": str(exc)}
            try:
                ack_command(command_id, "failed", error_result)
            except Exception as ack_exc:
                print(f"Failed to ack command {command_id}:", ack_exc)
            print(f"Command {command_id} failed:", exc)


def telemetry_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            camera_defaults = fetch_device_config()
            set_cached_camera_defaults(camera_defaults)

            telemetry = build_telemetry()
            result = post_telemetry(telemetry)
            print("Telemetry sent:", result)

            try:
                scheduled = schedule_due_recurring_jobs()
                if scheduled:
                    print("Recurring jobs scheduled:", len(scheduled))
            except Exception as exc:
                print("Recurring schedule error:", exc)

            try:
                snapshot = fetch_local_camera_snapshot(camera_defaults)
                camera_result = post_camera_snapshot(snapshot)
                if camera_result:
                    print("Camera snapshot uploaded:", camera_result)
            except Exception as exc:
                print("Camera upload error:", exc)
        except KeyboardInterrupt:
            print("Exporter stopped by user")
            stop_event.set()
            break
        except Exception as exc:
            print("Exporter error:", exc)

        if stop_event.wait(DATA_INTERVAL_SECONDS):
            break


def command_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            process_commands(get_cached_camera_defaults())
        except KeyboardInterrupt:
            print("Command poller stopped by user")
            stop_event.set()
            break
        except Exception as exc:
            print("Command processing error:", exc)

        if stop_event.wait(COMMAND_INTERVAL_SECONDS):
            break


def main():
    print(f"Starting Raspberry exporter for {DEVICE_NAME}")
    stop_event = threading.Event()

    try:
        set_cached_camera_defaults(fetch_device_config())
    except Exception as exc:
        print("Initial camera config error:", exc)

    telemetry_thread = threading.Thread(target=telemetry_loop, args=(stop_event,), daemon=True)
    command_thread = threading.Thread(target=command_loop, args=(stop_event,), daemon=True)

    telemetry_thread.start()
    command_thread.start()

    try:
        while telemetry_thread.is_alive() or command_thread.is_alive():
            telemetry_thread.join(timeout=1)
            command_thread.join(timeout=1)
    except KeyboardInterrupt:
        print("Exporter stopped by user")
        stop_event.set()
        telemetry_thread.join(timeout=5)
        command_thread.join(timeout=5)


if __name__ == "__main__":
    main()
