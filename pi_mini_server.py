#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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
    capture_camera_image(CAMERA_PATH)
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


@app.get("/test_camera_ui")
@app.get("/test_Camera_ui")
async def test_camera_ui():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Pi Camera Test</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111f;
      --panel: rgba(11, 21, 38, 0.92);
      --panel-2: rgba(16, 30, 52, 0.95);
      --text: #e8eef9;
      --muted: #8ea4c9;
      --accent: #6ee7b7;
      --border: rgba(148, 163, 184, 0.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(110, 231, 183, 0.14), transparent 28%),
        linear-gradient(180deg, #040816, var(--bg));
    }
    .wrap {
      max-width: 1100px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 18px;
      backdrop-filter: blur(14px);
      box-shadow: 0 18px 60px rgba(0,0,0,0.28);
      margin-bottom: 16px;
    }
    h1, h2, p { margin: 0; }
    h1 { font-size: clamp(2rem, 4vw, 3rem); letter-spacing: -0.04em; }
    .muted { color: var(--muted); }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    label {
      display: block;
      font-size: 0.88rem;
      color: var(--muted);
      margin-bottom: 8px;
    }
    input, select, button {
      width: 100%;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 11px 12px;
      font: inherit;
    }
    button {
      cursor: pointer;
      font-weight: 700;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .actions button {
      width: auto;
      padding-inline: 16px;
    }
    .primary {
      background: linear-gradient(135deg, #22c55e, #0ea5e9);
      border: none;
      color: #04111b;
    }
    .camera-shell {
      position: relative;
      border-radius: 20px;
      overflow: hidden;
      background: rgba(0,0,0,0.2);
      border: 1px solid var(--border);
      min-height: 320px;
    }
    img {
      width: 100%;
      height: 100%;
      min-height: 320px;
      display: block;
      object-fit: cover;
      background: rgba(0,0,0,0.12);
    }
    .status {
      margin-top: 10px;
      font-size: 0.92rem;
      color: var(--muted);
    }
    .small {
      font-size: 0.84rem;
      color: var(--muted);
    }
    .stack {
      display: grid;
      gap: 14px;
    }
    .preset-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .preset-row button {
      width: auto;
    }
    @media (max-width: 900px) {
      .grid, .row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card stack">
      <div>
        <h1>Camera Test</h1>
        <p class="muted">Use this page to tune color, brightness, and exposure on the Pi camera.</p>
      </div>
      <div>
        <label for="token">PI token</label>
        <input id="token" placeholder="Paste PI_TOKEN here" />
      </div>
      <div class="actions">
        <button class="primary" onclick="saveToken()">Save token</button>
        <button onclick="loadImage()">Capture now</button>
        <button onclick="startLive()">Start live refresh</button>
        <button onclick="stopLive()">Stop live refresh</button>
      </div>
      <div class="small" id="status">Idle</div>
    </div>

    <div class="card stack">
      <h2>Presets</h2>
      <div class="preset-row">
        <button onclick="applyPreset('default')">Default</button>
        <button onclick="applyPreset('bright')">Bright</button>
        <button onclick="applyPreset('dark')">Dark</button>
        <button onclick="applyPreset('warm')">Warm</button>
        <button onclick="applyPreset('cool')">Cool</button>
        <button onclick="applyPreset('indoor')">Indoor</button>
        <button onclick="applyPreset('plant')">Plant</button>
      </div>
    </div>

    <div class="card stack">
      <h2>Settings</h2>
      <div class="grid">
        <div>
          <label for="preset">Preset</label>
          <select id="preset">
            <option value="default">default</option>
            <option value="bright">bright</option>
            <option value="dark">dark</option>
            <option value="warm">warm</option>
            <option value="cool">cool</option>
            <option value="indoor">indoor</option>
            <option value="plant">plant</option>
          </select>
        </div>
        <div>
          <label for="awb">AWB</label>
          <input id="awb" placeholder="auto, incandescent, fluorescent..." />
        </div>
      </div>
      <div class="row">
        <div>
          <label for="brightness">Brightness</label>
          <input id="brightness" type="number" step="0.01" placeholder="0.0" />
        </div>
        <div>
          <label for="contrast">Contrast</label>
          <input id="contrast" type="number" step="0.01" placeholder="0.0" />
        </div>
        <div>
          <label for="saturation">Saturation</label>
          <input id="saturation" type="number" step="0.01" placeholder="0.0" />
        </div>
      </div>
      <div class="row">
        <div>
          <label for="sharpness">Sharpness</label>
          <input id="sharpness" type="number" step="0.01" placeholder="0.0" />
        </div>
        <div>
          <label for="ev">EV</label>
          <input id="ev" type="number" step="1" placeholder="0" />
        </div>
        <div>
          <label for="gain">Gain</label>
          <input id="gain" type="number" step="0.01" placeholder="0.0" />
        </div>
      </div>
      <div class="grid">
        <div>
          <label for="exposure">Exposure</label>
          <input id="exposure" placeholder="normal, auto, night..." />
        </div>
        <div>
          <label for="metering">Metering</label>
          <input id="metering" placeholder="average, spot..." />
        </div>
      </div>
      <div>
        <label for="shutter">Shutter</label>
        <input id="shutter" type="number" step="1" placeholder="microseconds" />
      </div>
    </div>

    <div class="card">
      <div class="camera-shell">
        <img id="preview" alt="Camera preview" />
      </div>
      <div class="status" id="capture-status">No capture yet</div>
    </div>
  </div>

  <script>
    let timer = null;

    function saveToken() {
      localStorage.setItem('pi-token', document.getElementById('token').value.trim());
      setStatus('Token saved');
    }

    function loadToken() {
      const token = localStorage.getItem('pi-token') || '';
      document.getElementById('token').value = token;
      return token;
    }

    function setStatus(text) {
      document.getElementById('status').textContent = text;
    }

    function setCaptureStatus(text) {
      document.getElementById('capture-status').textContent = text;
    }

    function buildUrl() {
      const params = new URLSearchParams();
      const fields = ['preset', 'brightness', 'contrast', 'saturation', 'sharpness', 'exposure', 'awb', 'metering', 'ev', 'shutter', 'gain'];
      for (const name of fields) {
        const value = document.getElementById(name).value.trim();
        if (value) params.set(name, value);
      }
      const token = loadToken();
      if (!token) {
        throw new Error('Missing PI token');
      }
      return `/test_camera?${params.toString()}`;
    }

    async function loadImage() {
      try {
        setStatus('Capturing...');
        const token = loadToken();
        if (!token) {
          setCaptureStatus('Paste PI token first.');
          setStatus('Missing token');
          return;
        }
        const url = buildUrl();
        const response = await fetch(url, {
          headers: { Authorization: `Bearer ${token}` },
          cache: 'no-store',
        });
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || response.statusText);
        }
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        const img = document.getElementById('preview');
        img.onload = () => URL.revokeObjectURL(objectUrl);
        img.src = objectUrl;
        setStatus('Loaded');
        setCaptureStatus(`Captured at ${new Date().toLocaleString()}`);
      } catch (err) {
        setStatus(`Error: ${err.message}`);
        setCaptureStatus(`Error: ${err.message}`);
      }
    }

    function startLive() {
      stopLive();
      loadImage();
      timer = window.setInterval(loadImage, 2000);
      setStatus('Live refresh running');
    }

    function stopLive() {
      if (timer) window.clearInterval(timer);
      timer = null;
      setStatus('Live refresh stopped');
    }

    function applyPreset(name) {
      document.getElementById('preset').value = name;
      loadImage();
    }

    document.getElementById('token').value = loadToken();
  </script>
</body>
</html>
        """,
        media_type="text/html",
    )


@app.get("/")
async def index():
    return JSONResponse({"message": "Raspberry Pi mini-server is running", "version": "2.0"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("pi_mini_server:app", host=SERVER_HOST, port=SERVER_PORT)
