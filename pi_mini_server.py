#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
import signal
import shutil
import threading
import time
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
PI_CAMERA_PRESET = os.getenv("PI_CAMERA_PRESET", "indoor")
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
auto_off_timers: dict[str, threading.Timer] = {}
auto_off_lock = threading.Lock()
camera_refresh_lock = threading.Lock()
camera_refresh_thread: threading.Thread | None = None
camera_refresh_interval_seconds = int(os.getenv("PI_CAMERA_REFRESH_SECONDS", "10"))
camera_capture_timeout_seconds = int(os.getenv("PI_CAMERA_CAPTURE_TIMEOUT_SECONDS", "30"))
camera_last_refresh_at: datetime | None = None
camera_last_refresh_error: str | None = None
camera_params_lock = threading.Lock()
camera_capture_params: dict[str, object] = {}
camera_capture_command: str | None = None


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


def cancel_auto_off(command: str):
    with auto_off_lock:
        timer = auto_off_timers.pop(command, None)
    if timer is not None:
        timer.cancel()


def schedule_auto_off(command: str, seconds: int):
    seconds = max(1, int(seconds))

    def _turn_off():
        try:
            if command == "pompa":
                set_pump(False)
            elif command == "incalzire":
                set_heater(False)
            elif command == "racire":
                set_cooler(False)
        finally:
            with auto_off_lock:
                auto_off_timers.pop(command, None)

    cancel_auto_off(command)
    timer = threading.Timer(seconds, _turn_off)
    timer.daemon = True
    with auto_off_lock:
        auto_off_timers[command] = timer
    timer.start()


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
    selected = (preset or PI_CAMERA_PRESET or "default").lower()
    return presets.get(selected, presets["default"])


def append_if_present(args: list[str], flag: str, value):
    if value is None or value == "":
        return
    args.extend([flag, str(value)])


def resolve_camera_capture_command() -> str:
    global camera_capture_command

    if camera_capture_command:
        return camera_capture_command

    for candidate in ("rpicam-jpeg", "libcamera-jpeg", "libcamera-still", "raspistill"):
        if shutil.which(candidate):
            camera_capture_command = candidate
            return candidate

    raise HTTPException(
        status_code=503,
        detail="No camera capture binary found. Install rpicam-jpeg, libcamera-jpeg, libcamera-still, or raspistill.",
    )


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
    temp_output_path = f"{output_path}.tmp"
    cmd = [
        resolve_camera_capture_command(),
        "-o",
        temp_output_path,
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

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=camera_capture_timeout_seconds,
        )
        if not os.path.exists(temp_output_path):
            raise HTTPException(status_code=503, detail="Camera capture did not produce an image")
        os.replace(temp_output_path, output_path)
    except subprocess.TimeoutExpired as exc:
        if os.path.exists(temp_output_path):
            try:
                os.unlink(temp_output_path)
            except Exception:
                pass
        if os.path.exists(output_path):
            return
        raise HTTPException(status_code=503, detail="Camera capture timed out") from exc
    except subprocess.CalledProcessError as exc:
        if os.path.exists(temp_output_path):
            try:
                os.unlink(temp_output_path)
            except Exception:
                pass
        if os.path.exists(output_path):
            return
        detail = "Camera capture failed"
        if exc.stderr:
            detail = f"{detail}: {exc.stderr.strip()}"
        raise HTTPException(status_code=503, detail=detail) from exc


def refresh_camera_snapshot() -> bool:
    global camera_last_refresh_at, camera_last_refresh_error

    if not camera_refresh_lock.acquire(blocking=False):
        return False

    try:
        try:
            with camera_params_lock:
                params = dict(camera_capture_params)
            capture_camera_image(CAMERA_PATH, **params)
            camera_last_refresh_at = datetime.now(timezone.utc)
            camera_last_refresh_error = None
            return True
        except HTTPException as exc:
            camera_last_refresh_error = str(exc.detail)
            return False
        except Exception as exc:
            camera_last_refresh_error = str(exc)
            return False
    finally:
        camera_refresh_lock.release()


def camera_refresh_loop():
    # Keep the latest image warm in the background so request handlers never wait on the camera.
    while True:
        try:
            if not refresh_camera_snapshot() and camera_last_refresh_error:
                print(f"Camera refresh failed: {camera_last_refresh_error}")
        except Exception as exc:
            print(f"Camera refresh loop error: {exc}")
        time.sleep(max(5, camera_refresh_interval_seconds))


def ensure_camera_refresh_thread():
    global camera_refresh_thread
    if camera_refresh_thread and camera_refresh_thread.is_alive():
        return
    camera_refresh_thread = threading.Thread(target=camera_refresh_loop, daemon=True)
    camera_refresh_thread.start()


def remember_camera_params(request: Request):
    params = parse_camera_params(request)
    with camera_params_lock:
        camera_capture_params.clear()
        camera_capture_params.update(params)
    return params


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

    duration_seconds = parameters.get("duration_seconds")
    if is_on:
        if duration_seconds:
            schedule_auto_off(command, duration_seconds)
        else:
            cancel_auto_off(command)
    else:
        cancel_auto_off(command)

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
    params = remember_camera_params(request)
    ensure_camera_refresh_thread()

    fd, temp_path = tempfile.mkstemp(prefix="iotca-camera-", suffix=".jpg")
    os.close(fd)
    try:
        capture_camera_image(temp_path, **params)
        if not os.path.exists(temp_path):
            raise HTTPException(status_code=503, detail="No camera image available")
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


@app.get("/test_camera")
@app.get("/test_Camera")
async def test_camera(request: Request):
    """Capture a fresh image with tunable settings for camera calibration."""
    require_pi_token(request)
    params = remember_camera_params(request)
    fd, temp_path = tempfile.mkstemp(prefix="iotca-camera-", suffix=".jpg")
    os.close(fd)
    try:
        capture_camera_image(temp_path, **params)
        if not os.path.exists(temp_path):
            raise HTTPException(status_code=503, detail="No camera image available")
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


@app.on_event("startup")
async def startup_background_tasks():
    ensure_camera_refresh_thread()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("pi_mini_server:app", host=SERVER_HOST, port=SERVER_PORT)
