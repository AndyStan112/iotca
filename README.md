# IoTCA Remote Control Split

This project splits the system into three parts:

- `pi_mini_server.py` runs on the Pi and owns the hardware.
- `scripts/pi_exporter.py` runs on the Pi and periodically exports sensor state to the cloud database, then polls and executes pending commands.
- `server.py` runs in the cloud and provides the password-gated UI plus command storage.

The two Pi-side processes are intentional:

- the mini-server is the local hardware API
- the exporter is the scheduled bridge that reads the local API, writes telemetry to PostgreSQL, and pulls commands from the cloud

The database schema in `migrations/` is left unchanged.

## What talks to what

- The Pi mini-server exposes `/api/status`, `/api/data`, `/api/control`, and `/api/camera`.
- The exporter polls the Pi mini-server locally, then posts telemetry to the cloud server.
- The cloud server stores telemetry in `measurements` and command history in `commands`.
- When you log in to the cloud UI, the backend stores commands in the database and the Pi pulls them on its next export cycle.

## Why both Pi processes exist

Keeping hardware control and data export separate makes the Pi side easier to restart and debug.
If telemetry export breaks, the actuators and camera still work.
If the mini-server restarts, the exporter just retries on the next interval.

## Environment

### Cloud server

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
ADMIN_PASSWORD=choose-a-password
SERVER_HOST=0.0.0.0
SERVER_PORT=5000
SESSION_COOKIE_NAME=iotca_session
SESSION_TTL_SECONDS=86400
```


### Raspberry Pi shared env

Both Pi-side processes can use the same `.env` file on the Raspberry Pi.
`pi_mini_server.py` reads the hardware settings, and `scripts/pi_exporter.py` reads the telemetry and command settings from that same file.

```env
SERVER_URL=https://your-cloud-server.example.com
DEVICE_NAME=greenhouse-01
DEVICE_TOKEN=secret-device-token
LOCAL_PI_URL=http://127.0.0.1:6000
PI_TOKEN=secret-pi-token
DATA_INTERVAL_SECONDS=5
REQUEST_TIMEOUT=10.0
PI_SERVER_HOST=0.0.0.0
PI_SERVER_PORT=6000
PI_USB_HUB=3
PI_CAMERA_PATH=/tmp/local_plant.jpg
SENSOR_FALLBACK_TEMP=24.5
SENSOR_FALLBACK_HUMIDITY=55.0
```

## Install

```bash
uv sync --frozen
```

The dependencies are already declared in `pyproject.toml` and locked in `uv.lock`.
If you change dependencies later, update those files and run `uv sync` again.

## Apply migrations

```bash
uv run python3 scripts/db_create_tables.py
```

## Run the cloud server

```bash
uv run python3 -m uvicorn server:app --host 0.0.0.0 --port 5000
```

Open `/` in a browser, log in with `ADMIN_PASSWORD`, then use the dashboard to inspect telemetry and send commands.

## Run the Raspberry Pi mini-server

```bash
uv run python3 -m uvicorn pi_mini_server:app --host 0.0.0.0 --port 6000
```

## Run the Raspberry Pi exporter

```bash
uv run python3 scripts/pi_exporter.py
```

## Notes

- `old_server.py` is kept as a reference copy.
- The Pi mini-server keeps the hardware comments and control semantics from the old monolithic server.
- Commands are stored in the `commands` table and then claimed by the Pi exporter, so you get a small audit trail in the cloud UI without exposing the Pi directly.
