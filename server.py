#!/usr/bin/env python3
import base64
import json
import os
import secrets
import hmac
import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse, RedirectResponse

from iotca_store import get_recent_measurements as store_get_recent_measurements
from iotca_store import load_device_by_name as store_load_device_by_name
from iotca_store import summarize_recent_measurements
from mobile_ai import run_chat as run_mobile_chat
from mobile_ai import run_analysis as run_mobile_analysis

load_dotenv(dotenv_path=".env")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "iotca_session")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
SESSION_SECRET = os.getenv("SESSION_SECRET") or ADMIN_PASSWORD
ALLOWED_DEVICE_NAMES = {"greenhouse-01"}
CAMERA_CACHE_DIR = Path("/tmp/iotca_camera")
CAMERA_CACHE_DIR.mkdir(parents=True, exist_ok=True)

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required in .env")

if not ADMIN_PASSWORD:
    raise SystemExit("ADMIN_PASSWORD is required in .env")

app = FastAPI(title="IoT Control Server")


def get_db_connection():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@contextmanager
def db_session():
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _sign_session_payload(payload: str) -> str:
    secret = SESSION_SECRET.encode("utf-8")
    body = payload.encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        expires_at_str, _nonce = payload.split(":", 1)
        expires_at = int(expires_at_str)
    except ValueError:
        return False
    return expires_at >= int(time.time())


def get_request_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Session-Token") or request.cookies.get(SESSION_COOKIE_NAME)


def is_session_valid(token: str | None) -> bool:
    return _verify_session_token(token)


def require_admin_session(request: Request):
    token = get_request_token(request)
    if not is_session_valid(token):
        raise HTTPException(status_code=401, detail="Authentication required")
    return True


def create_session_response(payload: dict[str, Any], token: str | None = None) -> JSONResponse:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    session_payload = token or f"{expires_at}:{secrets.token_urlsafe(16)}"
    session_token = _sign_session_payload(session_payload)
    response = JSONResponse(payload)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return response


def clear_session_response(payload: dict[str, Any]) -> JSONResponse:
    response = JSONResponse(payload)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return response


def parse_timestamp(value: Any):
    if not value:
        return datetime.now(timezone.utc)
    if isinstance(value, str) and value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid recorded_at timestamp") from exc


def load_device_by_name(conn, device_name: str):
    with conn.cursor() as cur:
        cur.execute("SELECT id, device_name, device_key, metadata FROM devices WHERE device_name = %s", (device_name,))
        return cur.fetchone()


def update_device_metadata(conn, device_name: str, metadata: dict[str, Any]):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devices
            SET metadata = %s
            WHERE device_name = %s
            RETURNING id, device_name, device_key, metadata
            """,
            (psycopg2.extras.Json(metadata), device_name),
        )
        return cur.fetchone()


def camera_defaults_from_metadata(metadata: dict[str, Any] | None):
    defaults = {}
    if isinstance(metadata, dict):
        raw_defaults = metadata.get("camera_defaults")
        if isinstance(raw_defaults, dict):
            defaults.update(raw_defaults)
    return defaults


def ensure_device_for_device_token(conn, device_name: str, device_token: str, metadata: dict[str, Any] | None = None):
    if device_name not in ALLOWED_DEVICE_NAMES:
        raise HTTPException(status_code=403, detail="Device not allowed")
    existing = load_device_by_name(conn, device_name)
    with conn.cursor() as cur:
        if existing:
            if existing.get("device_key") and existing["device_key"] != device_token:
                raise HTTPException(status_code=401, detail="Invalid token for device")
            if metadata:
                merged = dict(existing.get("metadata") or {})
                merged.update(metadata)
                cur.execute(
                    "UPDATE devices SET device_key = COALESCE(device_key, %s), metadata = %s WHERE id = %s RETURNING id, device_name, device_key, metadata",
                    (device_token, psycopg2.extras.Json(merged), existing["id"]),
                )
                return cur.fetchone()
            return existing

        cur.execute(
            "INSERT INTO devices (device_name, device_key, metadata) VALUES (%s, %s, %s) RETURNING id, device_name, device_key, metadata",
            (device_name, device_token, psycopg2.extras.Json(metadata or {})),
        )
        return cur.fetchone()


def require_device_for_name(conn, device_name: str, device_token: str):
    if device_name not in ALLOWED_DEVICE_NAMES:
        raise HTTPException(status_code=403, detail="Device not allowed")
    device = load_device_by_name(conn, device_name)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    if not device.get("device_key") or device["device_key"] != device_token:
        raise HTTPException(status_code=401, detail="Invalid token for device")
    return device


def store_measurements(conn, device_id: int, recorded_at, metrics: list[dict[str, Any]]):
    rows = []
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        name = metric.get("metric")
        if not name:
            continue
        value = metric.get("value")
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        payload = metric.get("payload") or {}
        rows.append((device_id, recorded_at, name, value, psycopg2.extras.Json(payload)))

    if not rows:
        raise HTTPException(status_code=400, detail="No valid metrics were provided")

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO measurements (device_id, recorded_at, metric, value, payload) VALUES %s",
            rows,
        )

    return len(rows)


def get_latest_metrics(conn, device_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT recorded_at, metric, value, payload
            FROM measurements
            WHERE device_id = %s
            ORDER BY recorded_at DESC, id DESC
            LIMIT 200
            """,
            (device_id,),
        )
        rows = cur.fetchall()

    latest: dict[str, dict[str, Any]] = {}
    ordered = []
    for row in rows:
        metric = row["metric"]
        if metric in latest:
            continue
        latest[metric] = row
        ordered.append(row)
    return ordered


def get_recent_commands(conn, device_id: int, limit: int = 25):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, command, parameters, status, created_at, sent_at, acknowledged_at, result
            FROM commands
            WHERE device_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (device_id, limit),
        )
        return cur.fetchall()


def cancel_command(conn, command_id: int, device_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE commands
            SET status = 'canceled',
                result = %s
            WHERE id = %s AND device_id = %s AND status = 'pending'
            RETURNING id, command, parameters, status, created_at, sent_at, acknowledged_at, result
            """,
            (psycopg2.extras.Json({"canceled": True}), command_id, device_id),
        )
        return cur.fetchone()


def get_recurring_jobs(conn, device_id: int, limit: int = 100):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, job_name, command, parameters, interval_seconds, active, next_run_at, last_run_at, created_at, updated_at
            FROM recurring_jobs
            WHERE device_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (device_id, limit),
        )
        return cur.fetchall()


def insert_command(conn, device_id: int, command: str, parameters: dict[str, Any]):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO commands (device_id, command, parameters, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id, created_at
            """,
            (device_id, command, psycopg2.extras.Json(parameters)),
        )
        return cur.fetchone()


def create_recurring_job(conn, device_id: int, job_name: str, command: str, parameters: dict[str, Any], interval_seconds: int, active: bool = True):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recurring_jobs (
                device_id, job_name, command, parameters, interval_seconds, active, next_run_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s THEN now() ELSE now() + (%s || ' seconds')::interval END, now())
            RETURNING id, job_name, command, parameters, interval_seconds, active, next_run_at, last_run_at, created_at, updated_at
            """,
            (
                device_id,
                job_name,
                command,
                psycopg2.extras.Json(parameters),
                interval_seconds,
                active,
                active,
                interval_seconds,
            ),
        )
        return cur.fetchone()


def update_recurring_job_active(conn, job_id: int, device_id: int, active: bool):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recurring_jobs
            SET active = %s,
                next_run_at = CASE WHEN %s THEN now() ELSE next_run_at END,
                updated_at = now()
            WHERE id = %s AND device_id = %s
            RETURNING id, job_name, command, parameters, interval_seconds, active, next_run_at, last_run_at, created_at, updated_at
            """,
            (active, active, job_id, device_id),
        )
        return cur.fetchone()


def schedule_due_recurring_jobs(conn, device_id: int, limit: int = 20):
    scheduled = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, job_name, command, parameters, interval_seconds, next_run_at
            FROM recurring_jobs
            WHERE device_id = %s AND active = TRUE AND next_run_at <= now()
            ORDER BY next_run_at ASC, id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (device_id, limit),
        )
        rows = cur.fetchall()
        for row in rows:
            cur.execute(
                """
                INSERT INTO commands (device_id, command, parameters, status)
                VALUES (%s, %s, %s, 'pending')
                RETURNING id, created_at
                """,
                (device_id, row["command"], row["parameters"]),
            )
            command_row = cur.fetchone()
            cur.execute(
                """
                UPDATE recurring_jobs
                SET last_run_at = now(),
                    next_run_at = now() + make_interval(secs => interval_seconds),
                    updated_at = now()
                WHERE id = %s
                RETURNING id, job_name, command, parameters, interval_seconds, active, next_run_at, last_run_at, created_at, updated_at
                """,
                (row["id"],),
            )
            job_row = cur.fetchone()
            scheduled.append(
                {
                    "job": job_row,
                    "command": command_row,
                }
            )
    return scheduled


def get_pending_commands(conn, device_id: int, limit: int = 25):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, command, parameters, status, created_at, sent_at, acknowledged_at, result
            FROM commands
            WHERE device_id = %s AND status = 'pending'
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (device_id, limit),
        )
        return cur.fetchall()


def sanitize_device_name(device_name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in device_name)


def camera_snapshot_path(device_name: str) -> Path:
    return CAMERA_CACHE_DIR / f"{sanitize_device_name(device_name)}.jpg"


def leaf_favicon_svg() -> str:
    return """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1f3d2b"/>
      <stop offset="100%" stop-color="#0b1f19"/>
    </linearGradient>
    <radialGradient id="glow" cx="30%" cy="25%" r="80%">
      <stop offset="0%" stop-color="#7cf7c1" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#7cf7c1" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#bg)"/>
  <circle cx="22" cy="20" r="22" fill="url(#glow)"/>
  <text x="32" y="41" text-anchor="middle" font-size="28" font-family="Apple Color Emoji, Segoe UI Emoji, Noto Color Emoji, sans-serif">🌿</text>
</svg>
    """.strip()


MOBILE_DEVICE_NAME = "greenhouse-01"


def load_mobile_ai_context(window_points: int):
    window_points = max(1, int(window_points))
    with db_session() as conn:
        device = store_load_device_by_name(conn, MOBILE_DEVICE_NAME)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        recent_rows = store_get_recent_measurements(conn, MOBILE_DEVICE_NAME, limit=500)
        summary = summarize_recent_measurements(recent_rows, window_points=window_points)
        recurring_jobs = get_recurring_jobs(conn, device["id"], limit=100)

    return {
        "device_name": MOBILE_DEVICE_NAME,
        "window_points": window_points,
        "summary": summary,
        "latest_rows": recent_rows[-8:],
        "recurring_jobs": recurring_jobs,
    }


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    password = str(data.get("password") or "")
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid password")
    return create_session_response({"status": "ok"})


@app.post("/api/logout")
async def logout(request: Request):
    return clear_session_response({"status": "ok"})


@app.get("/api/me")
async def me(request: Request):
    return JSONResponse({"authenticated": is_session_valid(get_request_token(request))})


@app.get("/api/devices")
async def list_devices(request: Request, _: bool = Depends(require_admin_session)):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, device_name, metadata, created_at
                FROM devices
                ORDER BY created_at DESC, id DESC
                """
            )
            rows = cur.fetchall()
    return JSONResponse(jsonable_encoder({"devices": rows}))


@app.get("/api/device/config")
async def device_config(request: Request, device_name: str):
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        device_token = get_request_token(request)
        if not is_session_valid(device_token):
            if not device_token or device.get("device_key") != device_token:
                raise HTTPException(status_code=401, detail="Authentication required")
    metadata = device.get("metadata") or {}
    return JSONResponse(
        jsonable_encoder(
            {
                "device_name": device_name,
                "metadata": metadata,
                "camera_defaults": camera_defaults_from_metadata(metadata),
            }
        )
    )


@app.post("/api/device/camera-defaults")
async def set_device_camera_defaults(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    device_name = data.get("device_name")
    defaults = data.get("camera_defaults")
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not isinstance(defaults, dict):
        raise HTTPException(status_code=400, detail="camera_defaults must be an object")
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        metadata = dict(device.get("metadata") or {})
        metadata["camera_defaults"] = defaults
        device = update_device_metadata(conn, device_name, metadata)
    return JSONResponse(
        jsonable_encoder(
            {
                "status": "ok",
                "device_name": device_name,
                "metadata": device.get("metadata") if device else metadata,
                "camera_defaults": defaults,
            }
        )
    )


@app.post("/api/telemetry")
async def ingest_telemetry(request: Request):
    data = await request.json()
    device_name = data.get("device_name")
    device_token = get_request_token(request)
    metrics = data.get("metrics")
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")
    if not isinstance(metrics, list) or not metrics:
        raise HTTPException(status_code=400, detail="metrics must be a non-empty list")

    recorded_at = parse_timestamp(data.get("recorded_at"))
    metadata = {}
    if isinstance(data.get("metadata"), dict):
        metadata.update(data["metadata"])

    with db_session() as conn:
        device = ensure_device_for_device_token(conn, device_name, device_token, metadata=metadata)
        rows_saved = store_measurements(conn, device["id"], recorded_at, metrics)

    return JSONResponse(
        {
            "status": "ok",
            "device_name": device_name,
            "rows_saved": rows_saved,
            "timestamp": now_iso(),
        }
    )


@app.post("/api/camera/snapshot")
async def upload_camera_snapshot(request: Request):
    data = await request.json()
    device_name = data.get("device_name")
    image_b64 = data.get("image_base64")
    device_token = get_request_token(request)
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if device_name not in ALLOWED_DEVICE_NAMES:
        raise HTTPException(status_code=403, detail="Device not allowed")

    if not image_b64:
        raise HTTPException(status_code=400, detail="image_base64 is required")

    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid image_base64") from exc

    if not image_bytes:
        raise HTTPException(status_code=400, detail="image is required")

    with db_session() as conn:
        require_device_for_name(conn, device_name, device_token)

    snapshot_path = camera_snapshot_path(device_name)
    snapshot_path.write_bytes(image_bytes)
    return JSONResponse(
        {
            "status": "ok",
            "device_name": device_name,
            "bytes_saved": len(image_bytes),
            "timestamp": now_iso(),
        }
    )


@app.get("/api/camera/latest")
async def latest_camera_snapshot(request: Request, device_name: str, _: bool = Depends(require_admin_session)):
    if device_name not in ALLOWED_DEVICE_NAMES:
        raise HTTPException(status_code=403, detail="Device not allowed")
    snapshot_path = camera_snapshot_path(device_name)
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail="No camera snapshot available")
    return FileResponse(
        snapshot_path,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/recent")
async def recent_metrics(request: Request, device_name: str, metric: str | None = None, limit: int = 100, _: bool = Depends(require_admin_session)):
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        limit = max(1, min(limit, 500))
        with conn.cursor() as cur:
            if metric:
                cur.execute(
                    """
                    SELECT recorded_at, metric, value, payload
                    FROM measurements
                    WHERE device_id = %s AND metric = %s
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT %s
                    """,
                    (device["id"], metric, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT recorded_at, metric, value, payload
                    FROM measurements
                    WHERE device_id = %s
                    ORDER BY recorded_at DESC, id DESC
                    LIMIT %s
                    """,
                    (device["id"], limit),
                )
            rows = cur.fetchall()
    return JSONResponse(jsonable_encoder({"device_name": device_name, "rows": rows}))


@app.get("/api/latest")
async def latest_metrics(request: Request, device_name: str, _: bool = Depends(require_admin_session)):
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        rows = get_latest_metrics(conn, device["id"])
    return JSONResponse(jsonable_encoder({"device_name": device_name, "rows": rows}))


@app.get("/api/commands")
async def list_commands(request: Request, device_name: str, limit: int = 25, _: bool = Depends(require_admin_session)):
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        limit = max(1, min(limit, 100))
        rows = get_recent_commands(conn, device["id"], limit=limit)
    return JSONResponse(jsonable_encoder({"device_name": device_name, "rows": rows}))


@app.post("/api/commands/cancel")
async def cancel_pending_command(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    device_name = data.get("device_name")
    command_id = data.get("command_id")
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if command_id is None:
        raise HTTPException(status_code=400, detail="command_id is required")

    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        row = cancel_command(conn, int(command_id), device["id"])

    if not row:
        raise HTTPException(status_code=409, detail="Command is no longer pending")
    return JSONResponse(jsonable_encoder({"status": "ok", "command": row}))


@app.post("/api/commands/claim")
async def claim_commands(request: Request):
    data = await request.json()
    device_name = data.get("device_name")
    device_token = get_request_token(request)
    limit = int(data.get("limit") or 10)

    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")

    with db_session() as conn:
        device = require_device_for_name(conn, device_name, device_token)
        rows = get_pending_commands(conn, device["id"], limit=max(1, min(limit, 50)))
    return JSONResponse(jsonable_encoder({"device_name": device_name, "rows": rows}))


@app.post("/api/commands/ack")
async def ack_command(request: Request):
    data = await request.json()
    device_name = data.get("device_name")
    command_id = data.get("command_id")
    status = str(data.get("status") or "completed")
    result = data.get("result") or {}
    device_token = get_request_token(request)

    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")
    if command_id is None:
        raise HTTPException(status_code=400, detail="command_id is required")

    with db_session() as conn:
        device = require_device_for_name(conn, device_name, device_token)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE commands
                SET status = %s,
                    acknowledged_at = now(),
                    result = %s
                WHERE id = %s AND device_id = %s
                RETURNING id, command, parameters, status, created_at, sent_at, acknowledged_at, result
                """,
                (status, psycopg2.extras.Json(result), command_id, device["id"]),
            )
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Command not found")
    return JSONResponse(jsonable_encoder({"status": "ok", "command": row}))


@app.post("/api/command")
async def create_command(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    device_name = data.get("device_name")
    command = data.get("command")
    parameters = data.get("parameters") or {}

    if not device_name or not command:
        raise HTTPException(status_code=400, detail="device_name and command are required")
    if not isinstance(parameters, dict):
        raise HTTPException(status_code=400, detail="parameters must be an object")

    conn = get_db_connection()
    try:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO commands (device_id, command, parameters, status)
                VALUES (%s, %s, %s, 'pending')
                RETURNING id, created_at
                """,
                (device["id"], command, psycopg2.extras.Json(parameters)),
            )
            command_row = cur.fetchone()
        conn.commit()
    finally:
        conn.close()

    return JSONResponse(jsonable_encoder({"status": "ok", "command_id": command_row["id"], "command": command}))


@app.get("/api/recurring-jobs")
async def list_recurring_jobs(request: Request, device_name: str, _: bool = Depends(require_admin_session)):
    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        rows = get_recurring_jobs(conn, device["id"], limit=100)
    return JSONResponse(jsonable_encoder({"device_name": device_name, "rows": rows}))


@app.post("/api/recurring-jobs")
async def create_recurring_job_route(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    device_name = data.get("device_name")
    job_name = str(data.get("job_name") or "").strip()
    command = str(data.get("command") or "").strip()
    parameters = data.get("parameters") or {}
    interval_seconds = int(data.get("interval_seconds") or 0)
    active = bool(data.get("active", True))

    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not job_name:
        raise HTTPException(status_code=400, detail="job_name is required")
    if not command:
        raise HTTPException(status_code=400, detail="command is required")
    if not isinstance(parameters, dict):
        raise HTTPException(status_code=400, detail="parameters must be an object")
    if interval_seconds <= 0:
        raise HTTPException(status_code=400, detail="interval_seconds must be greater than zero")

    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        row = create_recurring_job(conn, device["id"], job_name, command, parameters, interval_seconds, active=active)

    return JSONResponse(jsonable_encoder({"status": "ok", "job": row}))


@app.post("/api/recurring-jobs/toggle")
async def toggle_recurring_job(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    device_name = data.get("device_name")
    job_id = data.get("job_id")
    active = data.get("active")
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if job_id is None:
        raise HTTPException(status_code=400, detail="job_id is required")
    if active is None:
        raise HTTPException(status_code=400, detail="active is required")

    with db_session() as conn:
        device = load_device_by_name(conn, device_name)
        if not device:
            raise HTTPException(status_code=404, detail="Device not found")
        row = update_recurring_job_active(conn, int(job_id), device["id"], bool(active))

    if not row:
        raise HTTPException(status_code=404, detail="Recurring job not found")
    return JSONResponse(jsonable_encoder({"status": "ok", "job": row}))


@app.post("/api/recurring-jobs/run-due")
async def run_due_recurring_jobs(request: Request):
    data = await request.json()
    device_name = data.get("device_name")
    device_token = get_request_token(request)
    limit = int(data.get("limit") or 20)
    if not device_name:
        raise HTTPException(status_code=400, detail="device_name is required")
    if not device_token:
        raise HTTPException(status_code=401, detail="Missing device token")

    with db_session() as conn:
        device = require_device_for_name(conn, device_name, device_token)
        scheduled = schedule_due_recurring_jobs(conn, device["id"], limit=max(1, min(limit, 50)))

    return JSONResponse(jsonable_encoder({"status": "ok", "device_name": device_name, "scheduled": scheduled}))


@app.get("/camera-test")
async def camera_test():
    return RedirectResponse(url="/#camera-calibration", status_code=307)


@app.get("/api/mobile/context")
async def mobile_context(window_points: int = 10, _: bool = Depends(require_admin_session)):
    return JSONResponse(jsonable_encoder({"status": "ok", **load_mobile_ai_context(window_points)}))


@app.post("/api/mobile/chat")
async def mobile_chat(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    message = str(data.get("message") or "").strip()
    window_points = int(data.get("window_points") or 10)
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    context = load_mobile_ai_context(window_points)
    snapshot_path = camera_snapshot_path(MOBILE_DEVICE_NAME)
    session_id = get_request_token(request)
    reply = await run_mobile_chat(
        database_url=DATABASE_URL,
        device_name=MOBILE_DEVICE_NAME,
        message=message,
        window_summary=context,
        latest_rows=context["latest_rows"],
        snapshot_path=snapshot_path if snapshot_path.exists() else None,
        session_id=session_id,
    )
    return JSONResponse({"status": "ok", "reply": reply})


@app.post("/api/mobile/analyze")
async def mobile_analyze(request: Request, _: bool = Depends(require_admin_session)):
    data = await request.json()
    message = str(data.get("message") or "").strip()
    window_points = int(data.get("window_points") or 10)
    context = load_mobile_ai_context(window_points)
    snapshot_path = camera_snapshot_path(MOBILE_DEVICE_NAME)
    session_id = get_request_token(request)
    analysis = await run_mobile_analysis(
        database_url=DATABASE_URL,
        device_name=MOBILE_DEVICE_NAME,
        message=message,
        window_summary=context,
        latest_rows=context["latest_rows"],
        snapshot_path=snapshot_path if snapshot_path.exists() else None,
        session_id=session_id,
    )
    return JSONResponse({"status": "ok", "analysis": analysis})


@app.get("/")
async def index():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <title>IoT Control Center</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #07111f;
      --panel: rgba(11, 21, 38, 0.88);
      --panel-2: rgba(16, 30, 52, 0.95);
      --text: #e8eef9;
      --muted: #8ea4c9;
      --accent: #6ee7b7;
      --accent-2: #f7b267;
      --danger: #f87171;
      --border: rgba(148, 163, 184, 0.18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(110, 231, 183, 0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(59, 130, 246, 0.16), transparent 24%),
        linear-gradient(180deg, #040816, var(--bg));
    }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 32px 16px 48px; }
    .hero {
      display: flex; align-items: end; justify-content: space-between; gap: 16px;
      margin-bottom: 20px;
    }
    .title { margin: 0; font-size: clamp(2rem, 5vw, 3.4rem); letter-spacing: -0.04em; }
    .subtitle { margin: 8px 0 0; color: var(--muted); max-width: 60ch; }
    .grid { display: grid; grid-template-columns: 1.5fr 1fr; gap: 16px; }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 18px;
      backdrop-filter: blur(14px);
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
    }
    .card h2, .card h3 { margin-top: 0; }
    .login { max-width: 480px; margin: 80px auto 0; }
    label { display: block; color: var(--muted); font-size: 0.9rem; margin-bottom: 8px; }
    input, select, button, textarea {
      width: 100%;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 12px 14px;
      font: inherit;
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .stack { display: grid; gap: 12px; }
    .actions { display: flex; gap: 12px; flex-wrap: wrap; }
    .actions button { width: auto; padding-inline: 16px; cursor: pointer; }
    .action-link {
      width: auto;
      padding: 12px 16px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(148, 163, 184, 0.12);
      color: var(--text);
    }
    .primary { background: linear-gradient(135deg, #22c55e, #0ea5e9); border: none; color: #04111b; font-weight: 700; }
    .ghost { background: rgba(148, 163, 184, 0.12); }
    .danger { background: rgba(248, 113, 113, 0.16); border-color: rgba(248, 113, 113, 0.32); }
    .kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
    .kpi {
      padding: 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
    }
    .kpi .label { color: var(--muted); font-size: 0.82rem; }
    .kpi .value { font-size: 1.5rem; margin-top: 8px; font-weight: 700; }
    .muted { color: var(--muted); }
    .hidden { display: none !important; }
    .divider { height: 1px; background: var(--border); margin: 16px 0; }
    .list { display: grid; gap: 8px; }
    .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; background: rgba(255,255,255,0.06); color: var(--muted); font-size: 0.82rem; }
    pre {
      white-space: pre-wrap; word-break: break-word; margin: 0;
      background: rgba(0,0,0,0.18); padding: 12px; border-radius: 14px; border: 1px solid var(--border);
    }
    .camera-shell {
      position: relative;
      border-radius: 20px;
      overflow: hidden;
      min-height: 320px;
      background: rgba(0, 0, 0, 0.22);
      border: 1px solid var(--border);
    }
    .camera-image {
      width: 100%;
      height: 100%;
      min-height: 320px;
      object-fit: cover;
      display: block;
      background: rgba(0, 0, 0, 0.12);
    }
    .camera-placeholder {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      pointer-events: none;
      text-align: center;
      padding: 16px;
    }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    @media (max-width: 900px) {
      .grid, .split, .kpis, .row { grid-template-columns: 1fr; }
      .hero { align-items: start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div id="login-card" class="card login">
      <h1 class="title">IoT Control Center</h1>
      <p class="subtitle">Log in with the admin password to view telemetry and queue commands in the cloud database.</p>
      <div class="stack">
        <div>
          <label for="password">Admin password</label>
          <input id="password" type="password" placeholder="Enter password" />
        </div>
        <div class="actions">
          <button class="primary" onclick="login()">Log in</button>
        </div>
        <div id="login-error" class="muted"></div>
      </div>
    </div>

    <div id="dashboard" class="hidden">
      <div class="hero">
        <div>
          <h1 class="title">IoT Control Center</h1>
          <p class="subtitle">Telemetry lives in Postgres. Commands are authenticated in the UI, then picked up by the Pi exporter on its next poll.</p>
        </div>
        <div class="actions">
          <button class="ghost" onclick="refreshAll()">Refresh</button>
          <button class="danger" onclick="logout()">Log out</button>
        </div>
      </div>

      <div class="grid">
        <section class="card">
          <h2>Device</h2>
          <div class="row">
            <div>
              <label for="device-select">Select device</label>
              <select id="device-select" onchange="onDeviceChange()"></select>
            </div>
            <div>
              <label for="metric-filter">Metric filter</label>
              <input id="metric-filter" placeholder="Optional metric name" oninput="debouncedRefresh()" />
            </div>
          </div>
          <div class="divider"></div>
          <div class="kpis">
            <div class="kpi"><div class="label">Temperature</div><div class="value" id="kpi-temp">--</div></div>
            <div class="kpi"><div class="label">Humidity</div><div class="value" id="kpi-hum">--</div></div>
            <div class="kpi"><div class="label">Light</div><div class="value" id="kpi-light">--</div></div>
            <div class="kpi"><div class="label">Pi Status</div><div class="value" id="kpi-status">--</div></div>
          </div>
          <div class="divider"></div>
          <h3>Latest metrics</h3>
          <div id="metrics-list" class="list"></div>
        </section>

        <section class="card">
          <h2>Command</h2>
          <div class="stack">
            <div>
              <label for="command-name">Command</label>
              <select id="command-name">
                <option value="pompa">Pump</option>
                <option value="incalzire">Heat</option>
                <option value="racire">Cool</option>
              </select>
            </div>
            <div class="row">
              <div>
                <label for="command-state">State</label>
                <select id="command-state">
                  <option value="on">On</option>
                  <option value="off">Off</option>
                </select>
              </div>
              <div>
                <label for="command-extra">Extra JSON</label>
                <input id="command-extra" placeholder='{"duration":10}' />
              </div>
            </div>
            <div class="actions">
              <button class="primary" onclick="sendCommand()">Send command</button>
            </div>
            <div id="command-result" class="muted"></div>
          </div>

          <div class="divider"></div>
          <h3>Recent commands</h3>
          <div id="commands-list" class="list"></div>
        </section>
      </div>

      <div class="split" style="margin-top:16px;">
        <section class="card">
          <h2>Recent telemetry rows</h2>
          <div id="recent-list" class="list"></div>
        </section>
        <section class="card">
          <h2>Session</h2>
          <pre id="session-info">Loading...</pre>
        </section>
      </div>

      <section class="card" style="margin-top:16px;">
        <div class="hero" style="margin-bottom:12px;">
          <div>
            <h2>Camera</h2>
            <p class="subtitle">Photo refresh is the default. Live feed is manual and only starts when you press the button.</p>
          </div>
          <div class="actions">
            <button class="ghost" onclick="refreshCamera()">Refresh photo</button>
            <button class="primary" onclick="toggleCameraLive()">Start live feed</button>
          </div>
        </div>
        <div class="camera-shell">
          <img id="camera-feed" class="camera-image" alt="Latest camera snapshot" />
          <div id="camera-placeholder" class="camera-placeholder">No snapshot yet</div>
        </div>
        <div id="camera-status" class="muted" style="margin-top:10px;">Photo mode</div>
      </section>

      <section id="camera-calibration" class="card" style="margin-top:16px;">
        <div class="hero" style="margin-bottom:12px;">
          <div>
            <h2>Camera calibration</h2>
            <p class="subtitle">Save the defaults here, then queue a capture command. The Pi worker will use the DB-backed profile on its next pass.</p>
          </div>
          <div class="actions">
            <button class="ghost" onclick="saveCameraDefaults()">Save defaults</button>
            <button class="primary" onclick="queueCameraCapture()">Capture test</button>
            <a class="action-link" href="/camera-test">Open test endpoint</a>
          </div>
        </div>
        <div class="split">
          <div class="stack">
            <div class="row">
              <div>
                <label for="camera-preset">Preset</label>
                <select id="camera-preset">
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
                <label for="camera-awb">AWB</label>
                <input id="camera-awb" placeholder="auto, incandescent, fluorescent..." />
              </div>
            </div>
            <div class="row">
              <div>
                <label for="camera-brightness">Brightness</label>
                <input id="camera-brightness" type="number" step="0.01" placeholder="0.0" />
              </div>
              <div>
                <label for="camera-contrast">Contrast</label>
                <input id="camera-contrast" type="number" step="0.01" placeholder="0.0" />
              </div>
              <div>
                <label for="camera-saturation">Saturation</label>
                <input id="camera-saturation" type="number" step="0.01" placeholder="0.0" />
              </div>
            </div>
            <div class="row">
              <div>
                <label for="camera-sharpness">Sharpness</label>
                <input id="camera-sharpness" type="number" step="0.01" placeholder="0.0" />
              </div>
              <div>
                <label for="camera-ev">EV</label>
                <input id="camera-ev" type="number" step="1" placeholder="0" />
              </div>
              <div>
                <label for="camera-gain">Gain</label>
                <input id="camera-gain" type="number" step="0.01" placeholder="0.0" />
              </div>
            </div>
            <div class="row">
              <div>
                <label for="camera-exposure">Exposure</label>
                <input id="camera-exposure" placeholder="normal, auto, night..." />
              </div>
              <div>
                <label for="camera-metering">Metering</label>
                <input id="camera-metering" placeholder="average, spot..." />
              </div>
              <div>
                <label for="camera-shutter">Shutter</label>
                <input id="camera-shutter" type="number" step="1" placeholder="microseconds" />
              </div>
            </div>
          </div>
          <div class="stack">
            <div class="camera-shell">
              <img id="camera-test-feed" class="camera-image" alt="Camera calibration preview" />
              <div id="camera-test-placeholder" class="camera-placeholder">No calibration capture yet</div>
            </div>
            <div id="camera-test-status" class="muted">Calibration idle</div>
          </div>
        </div>
      </section>

      <section id="recurring-jobs" class="card" style="margin-top:16px;">
        <div class="hero" style="margin-bottom:12px;">
          <div>
            <h2>Recurring jobs</h2>
            <p class="subtitle">Create a repeating command like watering every 30 seconds, then start or stop it without deleting the schedule.</p>
          </div>
          <div class="actions">
            <button class="ghost" onclick="loadRecurringJobs()">Refresh jobs</button>
          </div>
        </div>
        <div class="stack">
          <div class="row">
            <div>
              <label for="job-name">Job name</label>
              <input id="job-name" placeholder="Watering cycle" />
            </div>
            <div>
              <label for="job-command">Command</label>
              <select id="job-command">
                <option value="pompa">Pump</option>
                <option value="incalzire">Heat</option>
                <option value="racire">Cool</option>
              </select>
            </div>
          </div>
          <div class="row">
            <div>
              <label for="job-state">State</label>
              <select id="job-state">
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </div>
            <div>
              <label for="job-duration">Duration seconds</label>
              <input id="job-duration" type="number" min="1" step="1" placeholder="1" />
            </div>
            <div>
              <label for="job-interval">Interval seconds</label>
              <input id="job-interval" type="number" min="1" step="1" placeholder="30" />
            </div>
          </div>
          <div class="row">
            <div>
              <label for="job-extra">Extra JSON</label>
              <input id="job-extra" placeholder='{"notes":"optional"}' />
            </div>
            <div>
              <label for="job-active">Active on create</label>
              <select id="job-active">
                <option value="true" selected>Yes</option>
                <option value="false">No</option>
              </select>
            </div>
          </div>
          <div class="actions">
            <button class="primary" onclick="createRecurringJob()">Create recurring job</button>
          </div>
          <div id="jobs-result" class="muted"></div>
          <div id="jobs-list" class="list"></div>
        </div>
      </section>
    </div>
  </div>

<script>
  let refreshInterval = null;
  let debounceHandle = null;
  let cameraLiveTimer = null;
  let deviceMetadataByName = {};
  let recurringJobsById = {};

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { raw: text }; }
    if (!response.ok) {
      const error = data.detail || data.error || response.statusText;
      const err = new Error(error);
      err.status = response.status;
      throw err;
    }
    return data;
  }

  function showLogin(message = '') {
    if (refreshInterval) window.clearInterval(refreshInterval);
    if (debounceHandle) window.clearTimeout(debounceHandle);
    if (cameraLiveTimer) window.clearInterval(cameraLiveTimer);
    refreshInterval = null;
    debounceHandle = null;
    cameraLiveTimer = null;
    document.getElementById('login-card').classList.remove('hidden');
    document.getElementById('dashboard').classList.add('hidden');
    document.getElementById('login-error').textContent = message;
  }

  function showDashboard() {
    document.getElementById('login-card').classList.add('hidden');
    document.getElementById('dashboard').classList.remove('hidden');
  }

  async function login() {
    const password = document.getElementById('password').value;
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify({ password }) });
      await bootstrap();
    } catch (err) {
      showLogin(err.message);
    }
  }

  async function logout() {
    await api('/api/logout', { method: 'POST' });
    showLogin('');
  }

  async function bootstrap() {
    try {
      const me = await api('/api/me', { method: 'GET' });
      if (!me.authenticated) {
        showLogin('');
        return;
      }
      showDashboard();
      await loadDevices();
      await refreshAll();
      await refreshCamera();
      if (refreshInterval) window.clearInterval(refreshInterval);
      refreshInterval = window.setInterval(() => refreshAll().catch(handleRefreshError), 5000);
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        showLogin('');
        return;
      }
      showDashboard();
      document.getElementById('command-result').textContent = `Startup error: ${err.message}`;
      console.error(err);
    }
  }

  function handleRefreshError(err) {
    if (err && (err.status === 401 || err.status === 403)) {
      showLogin('');
      return;
    }
    document.getElementById('command-result').textContent = `Refresh error: ${err.message}`;
    console.error(err);
  }

  async function onDeviceChange() {
    const deviceName = currentDeviceName();
    populateCameraDefaults(deviceName);
    await refreshAll().catch(handleRefreshError);
    await refreshCamera().catch(handleRefreshError);
  }

  function currentDeviceName() {
    const select = document.getElementById('device-select');
    return select && select.value ? select.value : 'greenhouse-01';
  }

  function cameraUiStatus(message) {
    const status = document.getElementById('camera-status');
    if (status) status.textContent = message;
  }

  async function refreshCamera() {
    const img = document.getElementById('camera-feed');
    const placeholder = document.getElementById('camera-placeholder');
    if (!img) return;
    const deviceName = currentDeviceName();
    const url = `/api/camera/latest?device_name=${encodeURIComponent(deviceName)}&t=${Date.now()}`;
    img.onload = () => {
      if (placeholder) placeholder.style.display = 'none';
      cameraUiStatus(cameraLiveTimer ? 'Live feed running' : 'Photo mode');
    };
    img.onerror = () => {
      if (placeholder) placeholder.style.display = 'grid';
      cameraUiStatus('No snapshot yet');
    };
    img.src = url;
  }

  function toggleCameraLive() {
    if (cameraLiveTimer) {
      window.clearInterval(cameraLiveTimer);
      cameraLiveTimer = null;
      cameraUiStatus('Photo mode');
      const button = document.querySelector('button[onclick="toggleCameraLive()"]');
      if (button) button.textContent = 'Start live feed';
      return;
    }
    refreshCamera();
    cameraLiveTimer = window.setInterval(() => refreshCamera().catch(handleRefreshError), 2000);
    cameraUiStatus('Live feed running');
    const button = document.querySelector('button[onclick="toggleCameraLive()"]');
    if (button) button.textContent = 'Stop live feed';
  }

  async function queueCameraCapture() {
    const img = document.getElementById('camera-test-feed');
    const placeholder = document.getElementById('camera-test-placeholder');
    const status = document.getElementById('camera-test-status');
    const deviceName = currentDeviceName();
    if (status) status.textContent = 'Queueing calibration capture...';
    try {
      await api('/api/command', {
        method: 'POST',
        body: JSON.stringify({
          device_name: deviceName,
          command: 'camera_capture',
          parameters: {},
        }),
      });
      if (status) status.textContent = 'Calibration capture queued';
      if (placeholder) placeholder.style.display = 'grid';
      window.setTimeout(() => {
        if (img) {
          img.onload = () => {
            if (placeholder) placeholder.style.display = 'none';
            if (status) status.textContent = 'Calibration capture loaded';
          };
          img.onerror = () => {
            if (placeholder) placeholder.style.display = 'grid';
            if (status) status.textContent = 'Calibration capture failed';
          };
          img.src = `/api/camera/latest?device_name=${encodeURIComponent(deviceName)}&t=${Date.now()}`;
        }
      }, 7000);
    } catch (err) {
      if (status) status.textContent = err.message;
      if (err.status === 401 || err.status === 403) {
        showLogin('');
      }
    }
  }

  async function saveCameraDefaults() {
    const deviceName = currentDeviceName();
    const cameraDefaults = currentCameraDefaults();
    try {
      const result = await api('/api/device/camera-defaults', {
        method: 'POST',
        body: JSON.stringify({ device_name: deviceName, camera_defaults: cameraDefaults }),
      });
      deviceMetadataByName[deviceName] = result.metadata || {};
      const status = document.getElementById('camera-test-status');
      if (status) status.textContent = 'Saved as default camera profile';
    } catch (err) {
      const status = document.getElementById('camera-test-status');
      if (status) status.textContent = err.message;
      if (err.status === 401 || err.status === 403) {
        showLogin('');
      }
    }
  }

  async function loadDevices() {
    const data = await api('/api/devices', { method: 'GET' });
    const select = document.getElementById('device-select');
    select.innerHTML = '';
    deviceMetadataByName = {};
    for (const device of data.devices) {
      const option = document.createElement('option');
      option.value = device.device_name;
      option.textContent = device.device_name;
      select.appendChild(option);
      deviceMetadataByName[device.device_name] = device.metadata || {};
    }
    if (!select.value && select.options.length) select.value = select.options[0].value;
    populateCameraDefaults(select.value);
  }

  async function loadRecurringJobs() {
    const deviceName = currentDeviceName();
    if (!deviceName) return;
    const data = await api(`/api/recurring-jobs?device_name=${encodeURIComponent(deviceName)}`, { method: 'GET' });
    renderRecurringJobs(data.rows || []);
  }

  function renderRecurringJobs(rows) {
    const list = document.getElementById('jobs-list');
    recurringJobsById = {};
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = '<div class="muted">No recurring jobs yet.</div>';
      return;
    }
    list.innerHTML = rows.map(row => {
      recurringJobsById[row.id] = row;
      const nextRun = row.next_run_at ? `Next: ${row.next_run_at}` : 'Next: --';
      const lastRun = row.last_run_at ? `Last: ${row.last_run_at}` : 'Last: --';
      const buttonLabel = row.active ? 'Stop' : 'Start';
      return `
        <div class="pill">${row.job_name} · ${row.command} · every ${row.interval_seconds}s · ${row.active ? 'active' : 'paused'}</div>
        <pre>${JSON.stringify(row, null, 2)}</pre>
        <div class="actions">
          <button class="ghost" onclick="toggleRecurringJob(${row.id}, ${row.active ? 'false' : 'true'})">${buttonLabel}</button>
        </div>
        <div class="muted">${nextRun} · ${lastRun}</div>
      `;
    }).join('');
  }

  async function createRecurringJob() {
    const deviceName = currentDeviceName();
    const jobName = document.getElementById('job-name').value.trim();
    const command = document.getElementById('job-command').value;
    const state = document.getElementById('job-state').value;
    const durationRaw = document.getElementById('job-duration').value.trim();
    const intervalRaw = document.getElementById('job-interval').value.trim();
    const extraRaw = document.getElementById('job-extra').value.trim();
    const active = document.getElementById('job-active').value === 'true';

    if (!jobName) {
      document.getElementById('jobs-result').textContent = 'Job name is required.';
      return;
    }

    const intervalSeconds = Number.parseInt(intervalRaw, 10);
    if (!Number.isInteger(intervalSeconds) || intervalSeconds <= 0) {
      document.getElementById('jobs-result').textContent = 'Interval seconds must be a positive integer.';
      return;
    }

    let parameters = { state };
    if (durationRaw) {
      const durationSeconds = Number.parseInt(durationRaw, 10);
      if (!Number.isInteger(durationSeconds) || durationSeconds <= 0) {
        document.getElementById('jobs-result').textContent = 'Duration seconds must be a positive integer.';
        return;
      }
      parameters.duration_seconds = durationSeconds;
    }

    if (extraRaw) {
      try {
        const extra = JSON.parse(extraRaw);
        if (extra && typeof extra === 'object' && !Array.isArray(extra)) {
          parameters = { ...parameters, ...extra };
        } else {
          document.getElementById('jobs-result').textContent = 'Extra JSON must be an object.';
          return;
        }
      } catch (err) {
        document.getElementById('jobs-result').textContent = 'Invalid extra JSON.';
        return;
      }
    }

    try {
      const result = await api('/api/recurring-jobs', {
        method: 'POST',
        body: JSON.stringify({
          device_name: deviceName,
          job_name: jobName,
          command,
          parameters,
          interval_seconds: intervalSeconds,
          active,
        }),
      });
      document.getElementById('jobs-result').textContent = `Created recurring job ${result.job.id}.`;
      await loadRecurringJobs();
    } catch (err) {
      document.getElementById('jobs-result').textContent = err.message;
      if (err.status === 401 || err.status === 403) {
        showLogin('');
      }
    }
  }

  async function toggleRecurringJob(jobId, active) {
    const deviceName = currentDeviceName();
    try {
      const result = await api('/api/recurring-jobs/toggle', {
        method: 'POST',
        body: JSON.stringify({ device_name: deviceName, job_id: jobId, active }),
      });
      recurringJobsById[jobId] = result.job || recurringJobsById[jobId];
      await loadRecurringJobs();
    } catch (err) {
      document.getElementById('jobs-result').textContent = err.message;
      if (err.status === 401 || err.status === 403) {
        showLogin('');
      }
    }
  }

  function applyCameraDefaults(defaults = {}) {
    const fields = {
      'camera-preset': defaults.preset,
      'camera-brightness': defaults.brightness,
      'camera-contrast': defaults.contrast,
      'camera-saturation': defaults.saturation,
      'camera-sharpness': defaults.sharpness,
      'camera-exposure': defaults.exposure,
      'camera-awb': defaults.awb,
      'camera-metering': defaults.metering,
      'camera-ev': defaults.ev,
      'camera-shutter': defaults.shutter,
      'camera-gain': defaults.gain,
    };
    for (const [id, value] of Object.entries(fields)) {
      const input = document.getElementById(id);
      if (input) input.value = value ?? '';
    }
  }

  function populateCameraDefaults(deviceName) {
    if (!deviceName) return;
    const metadata = deviceMetadataByName[deviceName] || {};
    applyCameraDefaults(metadata.camera_defaults || {});
  }

  function currentCameraDefaults() {
    return {
      preset: document.getElementById('camera-preset').value.trim(),
      brightness: document.getElementById('camera-brightness').value.trim(),
      contrast: document.getElementById('camera-contrast').value.trim(),
      saturation: document.getElementById('camera-saturation').value.trim(),
      sharpness: document.getElementById('camera-sharpness').value.trim(),
      exposure: document.getElementById('camera-exposure').value.trim(),
      awb: document.getElementById('camera-awb').value.trim(),
      metering: document.getElementById('camera-metering').value.trim(),
      ev: document.getElementById('camera-ev').value.trim(),
      shutter: document.getElementById('camera-shutter').value.trim(),
      gain: document.getElementById('camera-gain').value.trim(),
    };
  }

  async function refreshAll() {
    const deviceName = document.getElementById('device-select').value;
    if (!deviceName) return;
    const metric = document.getElementById('metric-filter').value.trim();

    const latest = await api(`/api/latest?device_name=${encodeURIComponent(deviceName)}`, { method: 'GET' });
    renderMetrics(latest.rows || []);

    const recent = await api(`/api/recent?device_name=${encodeURIComponent(deviceName)}&limit=12${metric ? `&metric=${encodeURIComponent(metric)}` : ''}`, { method: 'GET' });
    renderRecent(recent.rows || []);

    const commands = await api(`/api/commands?device_name=${encodeURIComponent(deviceName)}&limit=10`, { method: 'GET' });
    renderCommands(commands.rows || []);

    const recurringJobs = await api(`/api/recurring-jobs?device_name=${encodeURIComponent(deviceName)}`, { method: 'GET' });
    renderRecurringJobs(recurringJobs.rows || []);

    document.getElementById('session-info').textContent = JSON.stringify({
      device_name: deviceName,
      metrics_returned: latest.rows?.length || 0,
      recent_rows: recent.rows?.length || 0,
      commands: commands.rows?.length || 0,
      recurring_jobs: recurringJobs.rows?.length || 0,
    }, null, 2);
  }

  function debouncedRefresh() {
    if (debounceHandle) window.clearTimeout(debounceHandle);
    debounceHandle = window.setTimeout(() => refreshAll().catch(handleRefreshError), 350);
  }

  function renderMetrics(rows) {
    const map = {};
    let newestTimestamp = null;
    for (const row of rows) map[row.metric] = row.value;
    for (const row of rows) {
      const ts = Date.parse(row.recorded_at);
      if (!Number.isNaN(ts) && (newestTimestamp === null || ts > newestTimestamp)) {
        newestTimestamp = ts;
      }
    }
    document.getElementById('kpi-temp').textContent = map.temperatura ?? '--';
    document.getElementById('kpi-hum').textContent = map.umiditate ?? '--';
    document.getElementById('kpi-light').textContent = map.lumina ?? '--';
    const ageSeconds = newestTimestamp === null ? null : Math.round((Date.now() - newestTimestamp) / 1000);
    let status = 'offline';
    if (ageSeconds !== null) {
      if (ageSeconds <= 15) {
        status = 'online';
      } else if (ageSeconds <= 120) {
        status = `stale (${ageSeconds}s)`;
      } else {
        status = `offline (${ageSeconds}s)`;
      }
    }
    document.getElementById('kpi-status').textContent = status;

    const list = document.getElementById('metrics-list');
    list.innerHTML = rows.map(row => `
      <div class="pill">${row.metric}</div>
      <pre>${JSON.stringify(row, null, 2)}</pre>
    `).join('');
  }

  function renderRecent(rows) {
    const list = document.getElementById('recent-list');
    list.innerHTML = rows.map(row => `
      <div class="pill">${row.metric} · ${row.recorded_at}</div>
      <pre>${JSON.stringify(row, null, 2)}</pre>
    `).join('');
  }

  function renderCommands(rows) {
    const list = document.getElementById('commands-list');
    list.innerHTML = rows.map(row => `
      <div class="pill">${row.command} · ${row.status}</div>
      <pre>${JSON.stringify(row, null, 2)}</pre>
      ${row.status === 'pending' ? `<div class="actions"><button class="ghost" onclick="cancelCommand(${row.id})">Cancel command</button></div>` : ''}
    `).join('');
  }

  async function sendCommand() {
    const deviceName = document.getElementById('device-select').value;
    const command = document.getElementById('command-name').value;
    const state = document.getElementById('command-state').value;
    const extraRaw = document.getElementById('command-extra').value.trim();
    let parameters = { state };
    if (extraRaw) {
      try {
        const extra = JSON.parse(extraRaw);
        parameters = { ...parameters, ...extra };
      } catch (err) {
        document.getElementById('command-result').textContent = 'Invalid extra JSON.';
        return;
      }
    }

    try {
      const result = await api('/api/command', {
        method: 'POST',
        body: JSON.stringify({ device_name: deviceName, command, parameters }),
      });
      document.getElementById('command-result').textContent = `Command ${result.command_id} sent successfully.`;
      await refreshAll();
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        showLogin('');
        return;
      }
      document.getElementById('command-result').textContent = err.message;
    }
  }

  async function cancelCommand(commandId) {
    const deviceName = document.getElementById('device-select').value;
    try {
      const result = await api('/api/commands/cancel', {
        method: 'POST',
        body: JSON.stringify({ device_name: deviceName, command_id: commandId }),
      });
      document.getElementById('command-result').textContent = `Command ${result.command.id} canceled.`;
      await refreshAll();
    } catch (err) {
      document.getElementById('command-result').textContent = err.message;
      if (err.status === 401 || err.status === 403) {
        showLogin('');
      }
    }
  }

  bootstrap().catch(console.error);
</script>
</body>
</html>
        """,
        media_type="text/html",
    )


@app.get("/mobile")
async def mobile():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <meta name="theme-color" content="#07111f" />
  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />
  <link rel="manifest" href="/mobile/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/mobile/icon.svg" />
  <title>IoT Control Mobile</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #050b14;
      --panel: rgba(13, 24, 41, 0.92);
      --panel-2: rgba(18, 33, 55, 0.96);
      --text: #e9f2ff;
      --muted: #93a9c9;
      --accent: #7cf7c1;
      --accent-2: #76b7ff;
      --warning: #ffd166;
      --danger: #ff8c8c;
      --border: rgba(148, 163, 184, 0.16);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(124, 247, 193, 0.16), transparent 28%),
        radial-gradient(circle at top left, rgba(118, 183, 255, 0.16), transparent 28%),
        linear-gradient(180deg, #030814, var(--bg));
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell {
      max-width: 980px;
      margin: 0 auto;
      padding: 18px 14px 40px;
      padding-bottom: calc(40px + env(safe-area-inset-bottom));
    }
    .hidden { display: none !important; }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 24px;
      padding: 16px;
      box-shadow: 0 18px 60px rgba(0, 0, 0, 0.28);
      backdrop-filter: blur(12px);
      margin-bottom: 14px;
    }
    .hero {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: end;
      margin-bottom: 10px;
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 1.85rem; letter-spacing: -0.04em; }
    .sub { color: var(--muted); margin-top: 6px; line-height: 1.45; }
    .stack { display: grid; gap: 12px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .metric {
      padding: 14px;
      border-radius: 20px;
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
    }
    .metric-name { color: var(--muted); font-size: 0.88rem; margin-bottom: 8px; }
    .metric-value { font-size: 1.5rem; font-weight: 800; line-height: 1.1; }
    .metric-meta { color: var(--muted); font-size: 0.86rem; margin-top: 8px; display: grid; gap: 3px; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.06);
      color: var(--muted);
      font-size: 0.84rem;
    }
    .controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button, input, select {
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 14px;
      font: inherit;
    }
    button {
      padding: 11px 14px;
      cursor: pointer;
    }
    .primary {
      background: linear-gradient(135deg, #2dd4bf, #60a5fa);
      color: #04111b;
      border: none;
      font-weight: 800;
    }
    .ghost { background: rgba(148, 163, 184, 0.12); }
    .danger { background: rgba(255, 140, 140, 0.16); border-color: rgba(255, 140, 140, 0.28); }
    .icon-btn {
      width: 58px;
      height: 58px;
      display: grid;
      place-items: center;
      font-size: 1.45rem;
      padding: 0;
    }
    .icon-row { display: flex; gap: 10px; flex-wrap: wrap; }
    .icon-group {
      display: grid;
      gap: 10px;
      padding: 14px;
      border-radius: 20px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.03);
    }
    .icon-group .label { color: var(--muted); font-size: 0.88rem; }
    .duration-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .duration-chip {
      padding: 9px 12px;
      border-radius: 999px;
      font-size: 0.9rem;
      background: rgba(255,255,255,0.06);
    }
    .duration-chip.active {
      border-color: rgba(124, 247, 193, 0.7);
      box-shadow: 0 0 0 2px rgba(124, 247, 193, 0.12) inset;
    }
    .duration-custom {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .duration-custom input, .duration-custom select {
      padding: 11px 12px;
    }
    .ai-controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .loading-indicator {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 0.88rem;
      min-height: 24px;
    }
    .spinner {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 2px solid rgba(124, 247, 193, 0.22);
      border-top-color: var(--accent);
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    .ai-controls textarea {
      min-height: 92px;
      resize: vertical;
    }
    .chat-log {
      display: grid;
      gap: 10px;
      margin-bottom: 12px;
      max-height: 280px;
      overflow: auto;
      padding-right: 4px;
    }
    .chat-bubble {
      padding: 12px 14px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
    }
    .chat-bubble.user {
      background: rgba(124, 247, 193, 0.08);
      border-color: rgba(124, 247, 193, 0.18);
    }
    .chat-bubble.assistant {
      background: rgba(118, 183, 255, 0.08);
      border-color: rgba(118, 183, 255, 0.18);
    }
    .chat-role {
      font-size: 0.78rem;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .analysis-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .analysis-item {
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.04);
    }
    .analysis-item.full {
      grid-column: 1 / -1;
    }
    .analysis-item .label {
      color: var(--muted);
      font-size: 0.82rem;
      margin-bottom: 8px;
    }
    .analysis-item ul {
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 6px;
    }
    .analysis-badge {
      display: inline-flex;
      align-items: center;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(124, 247, 193, 0.12);
      border: 1px solid rgba(124, 247, 193, 0.2);
      font-size: 0.8rem;
      margin-bottom: 8px;
    }
    .mic-btn.listening {
      box-shadow: 0 0 0 2px rgba(255, 209, 102, 0.24) inset;
      border-color: rgba(255, 209, 102, 0.5);
    }
    .chart {
      width: 100%;
      height: 180px;
      display: block;
      background: rgba(0, 0, 0, 0.12);
      border-radius: 18px;
      border: 1px solid var(--border);
    }
    .chart-wrap {
      position: relative;
      width: 100%;
    }
    .chart-tooltip {
      position: absolute;
      pointer-events: none;
      transform: translate(-50%, calc(-100% - 10px));
      background: rgba(4, 10, 20, 0.96);
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 12px;
      padding: 8px 10px;
      min-width: 120px;
      color: var(--text);
      font-size: 0.82rem;
      box-shadow: 0 16px 32px rgba(0, 0, 0, 0.34);
      z-index: 2;
      display: none;
    }
    .chart-tooltip .label {
      color: var(--muted);
      font-size: 0.74rem;
      margin-bottom: 4px;
    }
    .chart-tooltip .value {
      font-weight: 700;
    }
    .metric-title-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
      margin-bottom: 10px;
    }
    .metric-title-row h2 { font-size: 1.05rem; }
    .camera-shell {
      position: relative;
      border-radius: 20px;
      overflow: hidden;
      min-height: 220px;
      border: 1px solid var(--border);
      background: rgba(0,0,0,0.18);
    }
    .camera-image {
      width: 100%;
      min-height: 220px;
      object-fit: cover;
      display: block;
      background: rgba(0,0,0,0.12);
    }
    .camera-placeholder {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      padding: 14px;
    }
    .login {
      max-width: 420px;
      margin: 64px auto 0;
    }
    label { display:block; color: var(--muted); font-size: 0.9rem; margin-bottom: 8px; }
    input, select {
      width: 100%;
      padding: 12px 14px;
    }
    .small { font-size: 0.85rem; color: var(--muted); }
    .spaced { display: grid; gap: 12px; }
    .status-line {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .subtitle { color: var(--muted); line-height: 1.45; }
    @media (max-width: 720px) {
      .grid { grid-template-columns: 1fr; }
      .hero { flex-direction: column; align-items: stretch; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section id="login-card" class="card login">
      <div class="stack">
        <div>
          <h1>IoT Control</h1>
          <p class="sub">Mobile dashboard with human-readable stats, graphs, and quick controls.</p>
        </div>
        <div>
          <label for="mobile-password">Admin password</label>
          <input id="mobile-password" type="password" placeholder="Enter password" />
        </div>
        <button class="primary" onclick="login()">Log in</button>
        <div id="mobile-login-error" class="small"></div>
      </div>
    </section>

    <main id="app" class="hidden">
      <section class="card">
        <div class="hero">
          <div>
            <h1>IoT Control</h1>
            <p class="sub">Temperature, humidity, and light are shown in plain language. Graphs use a smoothing window you can choose.</p>
          </div>
          <div class="controls">
            <button class="ghost" onclick="refreshAll()">Refresh</button>
            <button class="danger" onclick="logout()">Log out</button>
          </div>
        </div>
        <div class="status-line">
          <div class="badge" id="sync-status">Syncing...</div>
          <div class="badge">Device: <span id="device-name">greenhouse-01</span></div>
        </div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <h2>Current stats</h2>
          <div class="duration-row">
            <span class="badge">Smoothing window</span>
            <select id="window-size" onchange="refreshAll()">
              <option value="5">5 points</option>
              <option value="10" selected>10 points</option>
              <option value="20">20 points</option>
              <option value="50">50 points</option>
              <option value="100">100 points</option>
            </select>
          </div>
        </div>
        <div id="metrics-grid" class="grid"></div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <div>
            <h2>AI assistant</h2>
            <p class="small">Chat is plain text. Analysis uses the current plant image plus the selected smoothing window.</p>
          </div>
          <div class="badge" id="speech-status">Speech: idle</div>
        </div>
        <div class="stack">
          <div id="chat-log" class="chat-log"></div>
          <textarea id="ai-input" placeholder="Ask about the plant, the readings, or what to do next..."></textarea>
          <div class="ai-controls">
            <button id="chat-btn" class="primary" onclick="sendChat()">Send chat</button>
            <button id="mic-btn" class="ghost mic-btn" onclick="toggleSpeechInput()">🎙️ Voice input</button>
            <button id="analysis-btn" class="ghost" onclick="sendAnalysis()">AI analysis</button>
          </div>
          <div id="ai-loading" class="loading-indicator" aria-live="polite"></div>
        </div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <div>
            <h2>AI analysis</h2>
            <p class="small">Structured output rendered as cards, not raw JSON.</p>
          </div>
        </div>
        <div id="analysis-result" class="analysis-grid"></div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <div>
            <h2>Actuators</h2>
            <p class="small">Use icon buttons. Duration presets and a custom timer are available for timed ON actions.</p>
          </div>
          <div class="duration-row" id="duration-presets"></div>
        </div>
        <div class="duration-custom" style="margin-bottom: 12px;">
          <input id="custom-duration-value" type="number" min="1" step="1" placeholder="Custom duration" />
          <select id="custom-duration-unit">
            <option value="seconds">sec</option>
            <option value="minutes" selected>min</option>
            <option value="hours">hr</option>
          </select>
        </div>
        <div class="icon-row">
          <div class="icon-group">
            <div class="label">Pump</div>
            <div class="icon-row">
              <button class="icon-btn primary" title="Pump on" aria-label="Pump on" onclick="sendActuator('pompa', 'on')">💧</button>
              <button class="icon-btn ghost" title="Pump off" aria-label="Pump off" onclick="sendActuator('pompa', 'off')">⏻</button>
            </div>
          </div>
          <div class="icon-group">
            <div class="label">Heat</div>
            <div class="icon-row">
              <button class="icon-btn primary" title="Heat on" aria-label="Heat on" onclick="sendActuator('incalzire', 'on')">🔥</button>
              <button class="icon-btn ghost" title="Heat off" aria-label="Heat off" onclick="sendActuator('incalzire', 'off')">⏻</button>
            </div>
          </div>
          <div class="icon-group">
            <div class="label">Cool</div>
            <div class="icon-row">
              <button class="icon-btn primary" title="Cool on" aria-label="Cool on" onclick="sendActuator('racire', 'on')">❄️</button>
              <button class="icon-btn ghost" title="Cool off" aria-label="Cool off" onclick="sendActuator('racire', 'off')">⏻</button>
            </div>
          </div>
        </div>
        <div id="command-feedback" class="small" style="margin-top:12px;"></div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <div>
            <h2>Graphs</h2>
            <p class="small">Smoothed over the selected window, with mean and standard deviation based on the same window.</p>
          </div>
        </div>
        <div id="chart-list" class="stack"></div>
      </section>

      <section class="card">
        <div class="hero">
          <div>
            <h2>Camera</h2>
            <p class="sub">Photo refresh is the default. Live feed only starts when you press the button.</p>
          </div>
          <div class="controls">
            <button class="ghost" onclick="refreshCamera()">Refresh photo</button>
            <button class="primary" onclick="toggleCameraLive()">Start live feed</button>
          </div>
        </div>
        <div class="camera-shell">
          <img id="camera-feed" class="camera-image" alt="Latest camera snapshot" />
          <div id="camera-placeholder" class="camera-placeholder">No snapshot yet</div>
        </div>
        <div id="camera-status" class="small" style="margin-top:10px;">Photo mode</div>
      </section>

      <section class="card">
        <div class="status-line" style="margin-bottom:12px;">
          <div>
            <h2>Scheduled jobs</h2>
            <p class="small">Recurring jobs stored in Postgres, including watering cycles and other repeating commands.</p>
          </div>
          <div class="badge" id="jobs-status">Loading...</div>
        </div>
        <div id="mobile-job-list" class="spaced"></div>
      </section>
    </main>
  </div>

<script>
  const DEVICE_NAME = 'greenhouse-01';
  const PRESETS = [
    { label: '1s', seconds: 1 },
    { label: '3s', seconds: 3 },
    { label: '10s', seconds: 10 },
    { label: '1m', seconds: 60 },
  ];
  const METRICS = [
    { key: 'temperatura', label: 'Temperature', unit: '°C', color: '#7cf7c1', format: v => `${v.toFixed(1)} °C`, source: v => v },
    { key: 'umiditate', label: 'Humidity', unit: '%', color: '#76b7ff', format: v => `${v.toFixed(1)} %`, source: v => v },
    { key: 'lumina', label: 'Light', unit: '% brightness', color: '#ffd166', format: v => `${Math.max(0, Math.min(100, Math.round(v)))} % brightness`, source: v => brightnessFromRaw(v) },
  ];

  let refreshTimer = null;
  let cameraTimer = null;
  let seriesData = {};
  let selectedPreset = PRESETS[0].seconds;
  let recognition = null;
  let listening = false;
  let chatHistory = [];
  let uiBusy = false;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const text = await response.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (err) { data = { raw: text }; }
    if (!response.ok) {
      const error = data.detail || data.error || response.statusText;
      const err = new Error(error);
      err.status = response.status;
      throw err;
    }
    return data;
  }

  function setSyncStatus(text) {
    const el = document.getElementById('sync-status');
    if (el) el.textContent = text;
  }

  function showLogin(message = '') {
    if (refreshTimer) window.clearInterval(refreshTimer);
    if (cameraTimer) window.clearInterval(cameraTimer);
    refreshTimer = null;
    cameraTimer = null;
    document.getElementById('login-card').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
    document.getElementById('mobile-login-error').textContent = message;
  }

  function showApp() {
    document.getElementById('login-card').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
  }

  function setLoading(message = '', busy = false) {
    uiBusy = busy;
    const area = document.getElementById('ai-loading');
    if (area) {
      area.innerHTML = busy
        ? `<span class="spinner"></span><span>${escapeHtml(message || 'Working...')}</span>`
        : '';
    }
    const chatBtn = document.getElementById('chat-btn');
    const analysisBtn = document.getElementById('analysis-btn');
    const micBtn = document.getElementById('mic-btn');
    if (chatBtn) chatBtn.disabled = busy;
    if (analysisBtn) analysisBtn.disabled = busy;
    if (micBtn) micBtn.disabled = busy;
  }

  function preserveScroll(fn) {
    const scroller = document.scrollingElement || document.documentElement;
    const top = scroller.scrollTop;
    const left = scroller.scrollLeft;
    return Promise.resolve()
      .then(fn)
      .finally(() => {
        window.requestAnimationFrame(() => {
          scroller.scrollTo({ top, left, behavior: 'auto' });
        });
      });
  }

  function populatePresetButtons() {
    const wrap = document.getElementById('duration-presets');
    wrap.innerHTML = '';
    for (const preset of PRESETS) {
      const btn = document.createElement('button');
      btn.className = `duration-chip ${selectedPreset === preset.seconds ? 'active' : ''}`;
      btn.textContent = preset.label;
      btn.onclick = () => {
        selectedPreset = preset.seconds;
        populatePresetButtons();
      };
      wrap.appendChild(btn);
    }
  }

  function appendChat(role, text) {
    const log = document.getElementById('chat-log');
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role}`;
    bubble.innerHTML = `<div class="chat-role">${role}</div><div>${escapeHtml(text).replace(/\\n/g, '<br>')}</div>`;
    log.appendChild(bubble);
    log.scrollTop = log.scrollHeight;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function renderAnalysis(analysis) {
    const target = document.getElementById('analysis-result');
    const safe = analysis || {};
    const status = safe.health_status || (safe.plant_looks_healthy ? 'healthy' : 'watch');
    const observations = Array.isArray(safe.observations) ? safe.observations : [];
    const suggestions = Array.isArray(safe.suggestions) ? safe.suggestions : [];
    const concerns = Array.isArray(safe.concerns) ? safe.concerns : [];
    target.innerHTML = `
      <div class="analysis-item full">
        <div class="analysis-badge">${safe.plant_looks_healthy ? 'Plant looks healthy' : 'Needs attention'}</div>
        <div class="metric-value" style="font-size:1.25rem;">${status}</div>
        <div class="small">Confidence: ${safe.confidence ?? '--'}%</div>
      </div>
      <div class="analysis-item full">
        <div class="label">Summary</div>
        <div>${escapeHtml(safe.summary || 'No summary returned.')}</div>
      </div>
      <div class="analysis-item">
        <div class="label">Observations</div>
        <ul>${observations.map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li>No observations returned.</li>'}</ul>
      </div>
      <div class="analysis-item">
        <div class="label">Suggestions</div>
        <ul>${suggestions.map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li>No suggestions returned.</li>'}</ul>
      </div>
      <div class="analysis-item full">
        <div class="label">Concerns</div>
        <ul>${concerns.map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li>No concerns returned.</li>'}</ul>
      </div>
    `;
  }

  function renderRecurringJobs(rows) {
    const target = document.getElementById('mobile-job-list');
    const status = document.getElementById('jobs-status');
    const jobs = Array.isArray(rows) ? rows : [];
    if (status) status.textContent = jobs.length ? `${jobs.length} job${jobs.length === 1 ? '' : 's'}` : 'No jobs';
    if (!target) return;
    if (!jobs.length) {
      target.innerHTML = '<div class="small">No scheduled jobs yet.</div>';
      return;
    }
    target.innerHTML = jobs.map(job => {
      const nextRun = job.next_run_at ? new Date(job.next_run_at).toLocaleString() : '--';
      const lastRun = job.last_run_at ? new Date(job.last_run_at).toLocaleString() : '--';
      const details = Object.entries(job.parameters || {}).map(([key, value]) => `${escapeHtml(key)}: ${escapeHtml(value)}`).join(', ');
      return `
        <div class="metric">
          <div class="metric-title-row">
            <div>
              <h2>${escapeHtml(job.job_name)}</h2>
              <div class="small">${escapeHtml(job.command)} · every ${job.interval_seconds}s</div>
            </div>
            <div class="badge">${job.active ? 'active' : 'paused'}</div>
          </div>
          <div class="small">Next run: ${escapeHtml(nextRun)}</div>
          <div class="small">Last run: ${escapeHtml(lastRun)}</div>
          <div class="small">Parameters: ${details || '--'}</div>
        </div>
      `;
    }).join('');
  }

  async function sendChat() {
    const input = document.getElementById('ai-input');
    const message = input.value.trim();
    if (!message) return;
    input.value = '';
    appendChat('user', message);
    chatHistory.push({ role: 'user', text: message });
    setLoading('Thinking...', true);
    try {
      const payload = await api('/api/mobile/chat', {
        method: 'POST',
        body: JSON.stringify({ message, window_points: Number(document.getElementById('window-size').value || 10) }),
      });
      const reply = payload.reply || '';
      appendChat('assistant', reply);
      chatHistory.push({ role: 'assistant', text: reply });
    } catch (err) {
      handleError(err);
      appendChat('assistant', `Error: ${err.message}`);
    } finally {
      setLoading('', false);
    }
  }

  async function sendAnalysis() {
    const message = document.getElementById('ai-input').value.trim();
    setLoading('Analyzing plant health...', true);
    try {
      const payload = await api('/api/mobile/analyze', {
        method: 'POST',
        body: JSON.stringify({ message, window_points: Number(document.getElementById('window-size').value || 10) }),
      });
      renderAnalysis(payload.analysis || {});
    } catch (err) {
      handleError(err);
      renderAnalysis({
        plant_looks_healthy: false,
        health_status: 'watch',
        summary: `Analysis failed: ${err.message}`,
        observations: [],
        suggestions: [],
        concerns: [err.message],
        confidence: 0,
      });
    } finally {
      setLoading('', false);
    }
  }

  function setSpeechStatus(message) {
    const badge = document.getElementById('speech-status');
    if (badge) badge.textContent = message;
  }

  function stopSpeech() {
    listening = false;
    const mic = document.getElementById('mic-btn');
    if (mic) mic.classList.remove('listening');
    setSpeechStatus('Speech: idle');
    if (recognition) {
      recognition.stop();
    }
  }

  function toggleSpeechInput() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      setSpeechStatus('Speech: unavailable in this browser');
      return;
    }
    if (!recognition) {
      recognition = new SR();
      recognition.lang = navigator.language || 'en-US';
      recognition.interimResults = true;
      recognition.continuous = false;
      recognition.onresult = event => {
        const transcript = Array.from(event.results).map(result => result[0].transcript).join(' ');
        document.getElementById('ai-input').value = transcript.trim();
      };
      recognition.onerror = event => {
        const errorName = event && event.error ? event.error : 'unknown error';
        setSpeechStatus(`Speech error: ${errorName}`);
        stopSpeech();
      };
      recognition.onend = () => stopSpeech();
    }
    if (listening) {
      stopSpeech();
      return;
    }
    listening = true;
    document.getElementById('mic-btn').classList.add('listening');
    setSpeechStatus('Speech: listening');
    recognition.start();
  }

  function durationSeconds() {
    const value = document.getElementById('custom-duration-value').value.trim();
    if (value) {
      const amount = Number(value);
      if (!Number.isFinite(amount) || amount <= 0) return null;
      const unit = document.getElementById('custom-duration-unit').value;
      if (unit === 'seconds') return Math.round(amount);
      if (unit === 'minutes') return Math.round(amount * 60);
      if (unit === 'hours') return Math.round(amount * 3600);
    }
    return selectedPreset;
  }

  async function login() {
    const password = document.getElementById('mobile-password').value;
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify({ password }) });
      await bootstrap();
    } catch (err) {
      showLogin(err.message);
    }
  }

  async function logout() {
    await api('/api/logout', { method: 'POST' });
    showLogin('');
  }

  async function bootstrap() {
    try {
      const me = await api('/api/me', { method: 'GET' });
      if (!me.authenticated) {
        showLogin('');
        return;
      }
      showApp();
      populatePresetButtons();
      configureSpeechUi();
      if (!chatHistory.length) {
        appendChat('assistant', 'Ask me about the plant, the recent readings, or press AI analysis for a structured health report.');
      }
      await loadAndRender();
      await refreshCamera();
      if (refreshTimer) window.clearInterval(refreshTimer);
      refreshTimer = window.setInterval(() => loadAndRender().catch(handleError), 5000);
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        showLogin('');
        return;
      }
      showApp();
      setSyncStatus(`Startup error: ${err.message}`);
    }
  }

  function handleError(err) {
    if (err && (err.status === 401 || err.status === 403)) {
      showLogin('');
      return;
    }
    setSyncStatus(`Error: ${err.message}`);
    console.error(err);
  }

  function configureSpeechUi() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const micBtn = document.getElementById('mic-btn');
    if (!SR) {
      setSpeechStatus('Speech: unavailable in this browser');
      if (micBtn) {
        micBtn.disabled = true;
        micBtn.title = 'This browser does not support the Web Speech API';
      }
      return;
    }
    setSpeechStatus('Speech: idle');
    if (micBtn) {
      micBtn.disabled = false;
      micBtn.title = 'Voice input via the browser speech API';
    }
  }

  function brightnessFromRaw(raw) {
    return Math.max(0, Math.min(100, ((255 - raw) / 255) * 100));
  }

  function describeLight(raw) {
    const brightness = brightnessFromRaw(raw);
    if (brightness >= 75) return `Bright (${brightness.toFixed(0)}%)`;
    if (brightness >= 35) return `Moderate (${brightness.toFixed(0)}%)`;
    return `Dim (${brightness.toFixed(0)}%)`;
  }

  function formatMetric(metric, value) {
    if (metric.key === 'lumina') return describeLight(value);
    return metric.format(value);
  }

  function formatStatistic(metric, value) {
    if (value == null) return '--';
    if (metric.key === 'lumina') return `${Math.max(0, Math.min(100, Math.round(value)))} % brightness`;
    return metric.format(value);
  }

  function extractSeries(rows) {
    const grouped = {};
    for (const metric of METRICS) grouped[metric.key] = [];
    for (const row of rows.slice().reverse()) {
      if (!grouped[row.metric]) continue;
      const metric = METRICS.find(m => m.key === row.metric);
      const raw = Number(row.value);
      if (!Number.isFinite(raw)) continue;
      grouped[row.metric].push({
        t: Date.parse(row.recorded_at),
        v: metric.source(row.value),
      });
    }
    return grouped;
  }

  function slidingAverage(values, windowSize) {
    const out = [];
    for (let i = 0; i < values.length; i++) {
      const start = Math.max(0, i - windowSize + 1);
      const slice = values.slice(start, i + 1);
      const sum = slice.reduce((acc, item) => acc + item.v, 0);
      out.push({ t: values[i].t, v: sum / slice.length });
    }
    return out;
  }

  function mean(values) {
    if (!values.length) return null;
    return values.reduce((acc, v) => acc + v, 0) / values.length;
  }

  function stddev(values) {
    if (values.length < 2) return 0;
    const m = mean(values);
    const variance = values.reduce((acc, v) => acc + ((v - m) ** 2), 0) / values.length;
    return Math.sqrt(variance);
  }

  function roundValue(metricKey, value) {
    if (metricKey === 'lumina') return Math.round(value);
    return Math.round(value * 10) / 10;
  }

  function renderMetrics(rows) {
    const latest = {};
    for (const metric of METRICS) latest[metric.key] = null;
    for (const row of rows.slice().reverse()) {
      if (latest[row.metric] == null) latest[row.metric] = Number(row.value);
    }

    const grid = document.getElementById('metrics-grid');
    grid.innerHTML = '';

    for (const metric of METRICS) {
      const values = (seriesData[metric.key] || []).map(p => p.v);
      const windowSize = Number(document.getElementById('window-size').value || 10);
      const recentValues = values.slice(-windowSize);
      const current = latest[metric.key];
      const metricMean = mean(recentValues);
      const metricStd = stddev(recentValues);

      const card = document.createElement('div');
      card.className = 'metric';
      card.innerHTML = `
        <div class="metric-name">${metric.label}</div>
        <div class="metric-value">${current == null ? '--' : formatMetric(metric, current)}</div>
        <div class="metric-meta">
          <div>Mean: ${formatStatistic(metric, metricMean)}</div>
          <div>Std dev: ${formatStatistic(metric, metricStd)}</div>
        </div>
      `;
      grid.appendChild(card);
    }
  }

  function drawChart(canvas, points, metric) {
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const paddingLeft = 54;
    const paddingRight = 16;
    const paddingTop = 16;
    const paddingBottom = 28;
    const plotWidth = Math.max(1, width - paddingLeft - paddingRight);
    const plotHeight = Math.max(1, height - paddingTop - paddingBottom);

    if (points.length < 2) {
      ctx.fillStyle = '#93a9c9';
      ctx.font = '14px sans-serif';
      ctx.fillText('No data yet', 16, 24);
      return;
    }

    const values = points.map(p => p.v);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = Math.max(1e-9, max - min);
    const xStep = plotWidth / (points.length - 1);
    const yFor = value => paddingTop + plotHeight - ((value - min) / range) * plotHeight;
    const xFor = index => paddingLeft + index * xStep;

    ctx.fillStyle = 'rgba(255,255,255,0.03)';
    ctx.fillRect(paddingLeft, paddingTop, plotWidth, plotHeight);

    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const ratio = i / 4;
      const y = paddingTop + plotHeight - ratio * plotHeight;
      ctx.beginPath();
      ctx.moveTo(paddingLeft, y);
      ctx.lineTo(width - paddingRight, y);
      ctx.stroke();

      const labelValue = min + ratio * (max - min);
      ctx.fillStyle = '#93a9c9';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
      ctx.fillText(formatChartValue(metric, labelValue), paddingLeft - 8, y);
    }

    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(paddingLeft, paddingTop + plotHeight);
    ctx.lineTo(width - paddingRight, paddingTop + plotHeight);
    ctx.stroke();

    ctx.strokeStyle = metric.color;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    points.forEach((point, idx) => {
      const x = xFor(idx);
      const y = yFor(point.v);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.fillStyle = 'rgba(255,255,255,0.14)';
    points.forEach((point, idx) => {
      const x = xFor(idx);
      const y = yFor(point.v);
      ctx.beginPath();
      ctx.arc(x, y, 2.2, 0, Math.PI * 2);
      ctx.fill();
    });

    const firstTs = points[0].t;
    const lastTs = points[points.length - 1].t;
    ctx.fillStyle = '#93a9c9';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText(formatTimeLabel(firstTs), paddingLeft, height - 18);
    ctx.textAlign = 'right';
    ctx.fillText(formatTimeLabel(lastTs), width - paddingRight, height - 18);

    canvas._chartState = {
      points,
      min,
      max,
      xFor,
      yFor,
      paddingLeft,
      paddingTop,
      plotWidth,
      plotHeight,
    };
  }

  function renderCharts() {
    const windowSize = Number(document.getElementById('window-size').value || 10);
    const list = document.getElementById('chart-list');
    list.innerHTML = '';

    for (const metric of METRICS) {
      const rawSeries = seriesData[metric.key] || [];
      const smoothed = slidingAverage(rawSeries, windowSize);
      const card = document.createElement('div');
      card.className = 'metric';
      card.innerHTML = `
        <div class="metric-title-row">
          <div>
            <h2>${metric.label}</h2>
            <div class="small">Smoothed over ${windowSize} points</div>
          </div>
          <div class="badge">${metric.unit}</div>
        </div>
        <div class="chart-wrap">
          <canvas class="chart"></canvas>
          <div class="chart-tooltip"></div>
        </div>
      `;
      list.appendChild(card);
      const canvas = card.querySelector('canvas');
      const tooltip = card.querySelector('.chart-tooltip');
      drawChart(canvas, smoothed, metric);
      attachChartHover(canvas, tooltip, metric);
    }
  }

  function formatChartValue(metric, value) {
    if (value == null || Number.isNaN(value)) return '--';
    if (metric && metric.key === 'lumina') {
      return `${Math.max(0, Math.min(100, Math.round(value)))} % brightness`;
    }
    return metric ? metric.format(Number(value)) : String(value);
  }

  function formatTimeLabel(ts) {
    if (!ts) return '';
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function attachChartHover(canvas, tooltip, metric) {
    const state = canvas._chartState;
    if (!state) return;

    const hide = () => {
      tooltip.style.display = 'none';
    };

    const showPoint = event => {
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      const { points, xFor, yFor } = state;
      let nearest = null;
      let nearestDist = Infinity;
      points.forEach((point, idx) => {
        const px = xFor(idx);
        const py = yFor(point.v);
        const dist = Math.hypot(px - x, py - y);
        if (dist < nearestDist) {
          nearestDist = dist;
          nearest = { point, idx, px, py };
        }
      });
      if (!nearest || nearestDist > 28) {
        hide();
        return;
      }
      tooltip.style.display = 'block';
      tooltip.style.left = `${nearest.px}px`;
      tooltip.style.top = `${nearest.py}px`;
      tooltip.innerHTML = `
        <div class="label">${escapeHtml(metric.label)}</div>
        <div class="value">${escapeHtml(formatChartValue(metric, nearest.point.v))}</div>
        <div class="small">${escapeHtml(new Date(nearest.point.t).toLocaleString())}</div>
      `;
    };

    canvas.addEventListener('mousemove', showPoint);
    canvas.addEventListener('mouseleave', hide);
    canvas.addEventListener('touchstart', event => {
      const touch = event.touches && event.touches[0];
      if (!touch) return;
      showPoint({ clientX: touch.clientX, clientY: touch.clientY });
    }, { passive: true });
  }

  async function loadAndRender() {
    return preserveScroll(async () => {
      const [latest, recent, context] = await Promise.all([
        api(`/api/latest?device_name=${encodeURIComponent(DEVICE_NAME)}`, { method: 'GET' }),
        api(`/api/recent?device_name=${encodeURIComponent(DEVICE_NAME)}&limit=500`, { method: 'GET' }),
        api(`/api/mobile/context?window_points=${encodeURIComponent(document.getElementById('window-size').value || 10)}`, { method: 'GET' }),
      ]);
      document.getElementById('device-name').textContent = DEVICE_NAME;
      seriesData = extractSeries(recent.rows || []);
      renderMetrics(recent.rows || []);
      renderCharts();
      renderRecurringJobs(context.recurring_jobs || []);

      const latestRows = latest.rows || [];
      let newestTimestamp = null;
      for (const row of latestRows) {
        const ts = Date.parse(row.recorded_at);
        if (!Number.isNaN(ts) && (newestTimestamp === null || ts > newestTimestamp)) newestTimestamp = ts;
      }
      if (newestTimestamp) {
        const ageSeconds = Math.round((Date.now() - newestTimestamp) / 1000);
        if (ageSeconds <= 15) setSyncStatus(`Online · ${ageSeconds}s old`);
        else if (ageSeconds <= 120) setSyncStatus(`Stale · ${ageSeconds}s old`);
        else setSyncStatus(`Offline · ${ageSeconds}s old`);
      } else {
        setSyncStatus('No telemetry yet');
      }
    });
  }

  function currentDeviceUrl() {
    return `/api/camera/latest?device_name=${encodeURIComponent(DEVICE_NAME)}&t=${Date.now()}`;
  }

  async function refreshCamera() {
    const img = document.getElementById('camera-feed');
    const placeholder = document.getElementById('camera-placeholder');
    if (!img) return;
    img.onload = () => {
      if (placeholder) placeholder.style.display = 'none';
      document.getElementById('camera-status').textContent = cameraTimer ? 'Live feed running' : 'Photo mode';
    };
    img.onerror = () => {
      if (placeholder) placeholder.style.display = 'grid';
      document.getElementById('camera-status').textContent = 'No snapshot yet';
    };
    img.src = currentDeviceUrl();
  }

  function toggleCameraLive() {
    const button = document.querySelector('button[onclick="toggleCameraLive()"]');
    if (cameraTimer) {
      window.clearInterval(cameraTimer);
      cameraTimer = null;
      if (button) button.textContent = 'Start live feed';
      document.getElementById('camera-status').textContent = 'Photo mode';
      return;
    }
    refreshCamera();
    cameraTimer = window.setInterval(() => refreshCamera().catch(handleError), 2000);
    if (button) button.textContent = 'Stop live feed';
    document.getElementById('camera-status').textContent = 'Live feed running';
  }

  async function sendActuator(command, state) {
    const duration = state === 'on' ? durationSeconds() : null;
    const parameters = { state };
    if (state === 'on' && duration) parameters.duration_seconds = duration;
    try {
      const result = await api('/api/command', {
        method: 'POST',
        body: JSON.stringify({ device_name: DEVICE_NAME, command, parameters }),
      });
      document.getElementById('command-feedback').textContent = `Queued ${command} ${state}.${duration ? ` Duration: ${duration}s.` : ''}`;
      await loadAndRender();
      return result;
    } catch (err) {
      if (err.status === 401 || err.status === 403) {
        showLogin('');
        return;
      }
      document.getElementById('command-feedback').textContent = err.message;
    }
  }

  populatePresetButtons();
  bootstrap().catch(console.error);
</script>
<script>
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/mobile/sw.js').catch(() => {});
  }
</script>
</body>
</html>
        """,
        media_type="text/html",
    )


@app.get("/mobile/manifest.webmanifest")
async def mobile_manifest():
    manifest = {
        "name": "IoT Control Mobile",
        "short_name": "IoT Mobile",
        "start_url": "/mobile",
        "scope": "/mobile",
        "display": "standalone",
        "background_color": "#050b14",
        "theme_color": "#07111f",
        "icons": [
            {"src": "/mobile/icon.svg", "sizes": "192x192", "type": "image/svg+xml", "purpose": "any maskable"},
            {"src": "/mobile/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"},
        ],
    }
    return Response(content=json.dumps(manifest), media_type="application/manifest+json")


@app.get("/mobile/icon.svg")
async def mobile_icon():
    svg = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#7cf7c1"/>
      <stop offset="100%" stop-color="#60a5fa"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="110" fill="#06111f"/>
  <circle cx="256" cy="256" r="170" fill="url(#g)" opacity="0.18"/>
  <path d="M256 112c-58 0-104 46-104 104 0 74 104 184 104 184s104-110 104-184c0-58-46-104-104-104zm0 136c-18 0-32-14-32-32s14-32 32-32 32 14 32 32-14 32-32 32z" fill="url(#g)"/>
</svg>
    """.strip()
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/favicon.svg")
async def favicon_svg():
    return Response(content=leaf_favicon_svg(), media_type="image/svg+xml")


@app.get("/favicon.ico")
async def favicon_ico():
    return Response(content=leaf_favicon_svg(), media_type="image/svg+xml")


@app.get("/mobile/sw.js")
async def mobile_sw():
    script = """
const CACHE = 'iotca-mobile-v1';
const SHELL = ['/mobile', '/mobile/manifest.webmanifest', '/mobile/icon.svg'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.mode === 'navigate' && url.pathname.startsWith('/mobile')) {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/mobile'))
    );
    return;
  }
  if (url.pathname.startsWith('/api/')) return;
  event.respondWith(
    caches.match(event.request).then(hit => hit || fetch(event.request))
  );
});
    """.strip()
    return Response(content=script, media_type="application/javascript")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "5000"))
    uvicorn.run("server:app", host=host, port=port)
