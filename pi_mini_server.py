#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
import signal
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
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

URMARESTE_HUB_USB = os.getenv("PI_USB_HUB", "3")  # Hub-ul pompei; default 3.

def run_uhubctl(action: str):
    cmd = ["uhubctl", "-l", URMARESTE_HUB_USB, "-a", action]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]

    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
    )


try:
    # Never block startup on sudo prompts or a flaky USB hub.
    run_uhubctl("0")
except Exception:
    pass

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


@contextmanager
def time_limit(seconds: int):
    def _handler(signum, frame):
        raise TimeoutError(f"operation timed out after {seconds}s")

    previous_handler = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

if board is not None:
    heater = digitalio.DigitalInOut(board.D17)
    heater.direction = digitalio.Direction.OUTPUT
    heater.value = True

    peltier = digitalio.DigitalInOut(board.D27)
    peltier.direction = digitalio.Direction.OUTPUT
    peltier.value = True

    # Senzori
    dht_device = adafruit_dht.DHT22(board.D4)
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
        with time_limit(2):
            bus.write_byte(PCF8591_ADDRESS, 0x40)
            bus.read_byte(PCF8591_ADDRESS)
            return bus.read_byte(PCF8591_ADDRESS)
    except Exception:
        return 127


def read_sensors():
    try:
        with time_limit(2):
            temp = dht_device.temperature if dht_device is not None else None
            humidity = dht_device.humidity if dht_device is not None else None
    except RuntimeError:
        temp = None
        humidity = None
    except TimeoutError:
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
    try:
        run_uhubctl(action)
    except Exception:
        pass
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


def camera_preset_args(preset: str | None):
    presets = {
        "default": [],
        "bright": ["--brightness", "0.35", "--contrast", "0.15", "--saturation", "0.05", "--awb", "auto"],
        "dark": ["--brightness", "0.05", "--contrast", "0.25", "--saturation", "0.1", "--awb", "auto"],
        "warm": ["--brightness", "0.15", "--contrast", "0.2", "--saturation", "0.25", "--awb", "incandescent"],
        "cool": ["--brightness", "0.15", "--contrast", "0.2", "--saturation", "0.2", "--awb", "fluorescent"],
        "indoor": ["--brightness", "0.2", "--contrast", "0.15", "--saturation", "0.15", "--awb", "auto", "--exposure", "normal"],
        "plant": ["--brightness", "0.2", "--contrast", "0.2", "--saturation", "0.3", "--awb", "auto", "--sharpness", "1.0"],
    }
    return presets.get((preset or "default").lower(), [])


def append_if_present(args: list[str], flag: str, value):
    if value is None or value == "":
        return
    args.extend([flag, str(value)])


def capture_camera_image(
    output_path: str,
    *,
    preset: str | None = None,
    brightness: float | None = None,
    contrast: float | None = None,
    saturation: float | None = None,
    sharpness: float | None = None,
    exposure: str | None = None,
    awb: str | None = None,
    metering: str | None = None,
    ev: int | None = None,
    shutter: int | None = None,
    gain: float | None = None,
):
    cmd = [
        "rpicam-jpeg",
        "-o",
        output_path,
        "-t",
        "500",
        "--width",
        "640",
        "--height",
        "480",
        "--nopreview",
    ]
    cmd.extend(camera_preset_args(preset))
    append_if_present(cmd, "--brightness", brightness)
    append_if_present(cmd, "--contrast", contrast)
    append_if_present(cmd, "--saturation", saturation)
    append_if_present(cmd, "--sharpness", sharpness)
    append_if_present(cmd, "--exposure", exposure)
    append_if_present(cmd, "--awb", awb)
    append_if_present(cmd, "--metering", metering)
    append_if_present(cmd, "--ev", ev)
    append_if_present(cmd, "--shutter", shutter)
    append_if_present(cmd, "--gain", gain)

    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
        timeout=15,
    )
    if not os.path.exists(output_path):
        raise HTTPException(status_code=500, detail="Camera capture failed")


def parse_camera_params(request: Request):
    query = request.query_params

    def parse_float(name: str):
        raw = query.get(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {name}: {raw}") from exc

    def parse_int(name: str):
        raw = query.get(name)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid {name}: {raw}") from exc

    return {
        "preset": query.get("preset"),
        "brightness": parse_float("brightness"),
        "contrast": parse_float("contrast"),
        "saturation": parse_float("saturation"),
        "sharpness": parse_float("sharpness"),
        "exposure": query.get("exposure"),
        "awb": query.get("awb"),
        "metering": query.get("metering"),
        "ev": parse_int("ev"),
        "shutter": parse_int("shutter"),
        "gain": parse_float("gain"),
    }


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
    params = parse_camera_params(request)
    capture_camera_image(CAMERA_PATH, **params)
    return FileResponse(CAMERA_PATH, media_type="image/jpeg")


@app.get("/test_camera")
@app.get("/test_Camera")
async def test_camera(request: Request):
    """Capture a fresh image with tunable settings for camera calibration."""
    require_pi_token(request)
    params = parse_camera_params(request)
    fd, temp_path = tempfile.mkstemp(prefix="iotca-camera-", suffix=".jpg")
    os.close(fd)
    try:
        capture_camera_image(temp_path, **params)
        return FileResponse(
            temp_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
            background=BackgroundTask(os.unlink, temp_path),
        )
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


@app.get("/")
async def index():
    return JSONResponse({"message": "Raspberry Pi mini-server is running", "version": "2.0"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("pi_mini_server:app", host=SERVER_HOST, port=SERVER_PORT)
