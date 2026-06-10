#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

SERVER_URL = os.getenv("SERVER_URL")
DEVICE_NAME = os.getenv("DEVICE_NAME", "raspberry-pi")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN")
LOCAL_PI_URL = os.getenv("LOCAL_PI_URL", "http://127.0.0.1:6000")
PI_TOKEN = os.getenv("PI_TOKEN")
DATA_INTERVAL_SECONDS = int(os.getenv("DATA_INTERVAL_SECONDS", "5"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "10.0"))

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


def fetch_local_status():
    url = LOCAL_PI_URL.rstrip("/") + "/api/status"
    response = requests.get(url, headers=PI_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    return payload.get("data") or {}


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
    response = requests.post(url, headers=HEADERS, json=data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def main():
    print(f"Starting Raspberry exporter for {DEVICE_NAME}")
    while True:
        try:
            telemetry = build_telemetry()
            result = post_telemetry(telemetry)
            print("Telemetry sent:", result)
        except KeyboardInterrupt:
            print("Exporter stopped by user")
            break
        except Exception as exc:
            print("Exporter error:", exc)

        time.sleep(DATA_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
