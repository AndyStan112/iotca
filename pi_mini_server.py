#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

PI_TOKEN = os.getenv("PI_TOKEN")
SERVER_HOST = os.getenv("PI_SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PI_SERVER_PORT", "6000"))
CAMERA_PATH = os.getenv("PI_CAMERA_PATH", "/tmp/local_plant.jpg")
SENSOR_FALLBACK_TEMP = float(os.getenv("SENSOR_FALLBACK_TEMP", "24.5"))
SENSOR_FALLBACK_HUMIDITY = float(os.getenv("SENSOR_FALLBACK_HUMIDITY", "55.0"))

if not PI_TOKEN:
    raise SystemExit("PI_TOKEN is required in .env")

app = FastAPI(title="Raspberry Pi Mini Server")

URMARESTE_HUB_USB = os.getenv("PI_USB_HUB", "3")  # Schimbă cu hub-ul tău (1 sau 3)

import board
import adafruit_dht
import digitalio
import smbus2

# Relee/MOSFET-uri Active-Low (True = OPRIT, False = PORNIT)
heater = None
peltier = None
dht_device = None
bus = None
PCF8591_ADDRESS = 0x48

pump_state = False

if board is not None:
    heater = digitalio.DigitalInOut(board.D17)
    heater.direction = digitalio.Direction.OUTPUT
    heater.value = True

    peltier = digitalio.DigitalInOut(board.D27)
    peltier.direction = digitalio.Direction.OUTPUT
    peltier.value = True

    # Senzori
    dht_device = adafruit_dht.DHT22(board.D24)
    bus = smbus2.SMBus(1)


def get_request_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-PI-Token")


def require_pi_token(request: Request):
    token = get_request_token(request)
    if token != PI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid PI token")
    return True


def read_light():
    try:
        if bus is None:
            return 127
        bus.write_byte(PCF8591_ADDRESS, 0x40)
        bus.read_byte(PCF8591_ADDRESS)
        return bus.read_byte(PCF8591_ADDRESS)
    except Exception:
        return 127


def read_sensors():
    try:
        temp = dht_device.temperature if dht_device is not None else None
        humidity = dht_device.humidity if dht_device is not None else None
    except RuntimeError:
        temp = None
        humidity = None

    if temp is None or humidity is None:
        # DHT22 mai dă rateuri de citire, returnăm valori de rezervă ca să nu crăpe exportul.
        temp = SENSOR_FALLBACK_TEMP
        humidity = SENSOR_FALLBACK_HUMIDITY

    return temp, humidity


def get_status_payload():
    temp, humidity = read_sensors()
    # Inversat pentru UI/API: True = funcționează.
    heating_on = not heater.value if heater is not None else False
    cooling_on = not peltier.value if peltier is not None else False

    return {
        "temperatura": temp,
        "umiditate": humidity,
        "lumina": read_light(),
        "pompa": pump_state,
        "incalzire": heating_on,
        "racire": cooling_on,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def set_pump(on: bool):
    global pump_state
    pump_state = on
    action = "1" if on else "0"
    subprocess.run(
        ["sudo", "uhubctl", "-l", URMARESTE_HUB_USB, "-a", action],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return {"pompa": on}


def set_heater(on: bool):
    if heater is not None:
        heater.value = not on  # Active-Low: False pornește curentul
    return {"incalzire": on}


def set_cooler(on: bool):
    if peltier is not None:
        peltier.value = not on  # Active-Low: False pornește curentul
    return {"racire": on}


def parse_bool_state(value: str | None):
    return str(value or "on").lower() == "on"


# --- RUTE API (BACKEND) ---

@app.get("/api/status")
async def status(request: Request):
    require_pi_token(request)
    return JSONResponse({"status": "ok", "data": get_status_payload()})


@app.get("/api/data")
async def get_data(request: Request):
    """Returnează datele de la senzori în format JSON."""
    require_pi_token(request)
    return JSONResponse(get_status_payload())


@app.post("/api/control")
async def control(request: Request):
    """Controlează componentele hardware din backend-ul cloud."""
    require_pi_token(request)
    data = await request.json()
    command = data.get("command") or data.get("actuator")
    parameters = data.get("parameters") or {}

    if not command:
        raise HTTPException(status_code=400, detail="command is required")

    state_value = parameters.get("state")
    is_on = parse_bool_state(state_value)

    if command == "pompa":
        result = set_pump(is_on)
    elif command == "incalzire":
        result = set_heater(is_on)
    elif command == "racire":
        result = set_cooler(is_on)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown command: {command}")

    return JSONResponse(
        {
            "status": "ok",
            "command": command,
            "parameters": parameters,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/camera")
async def get_image(request: Request):
    """Face o poză în timp real și o trimite direct către client."""
    require_pi_token(request)
    subprocess.run(
        [
            "rpicam-jpeg",
            "-o",
            CAMERA_PATH,
            "-t",
            "500",
            "--width",
            "640",
            "--height",
            "480",
            "--nopreview",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if not os.path.exists(CAMERA_PATH):
        raise HTTPException(status_code=500, detail="Camera capture failed")
    return FileResponse(CAMERA_PATH, media_type="image/jpeg")


@app.get("/")
async def index():
    return JSONResponse({"message": "Raspberry Pi mini-server is running", "version": "2.0"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("pi_mini_server:app", host=SERVER_HOST, port=SERVER_PORT)
