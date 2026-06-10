#!/usr/bin/env python3
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv(dotenv_path=".env")

DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "iotca_session")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required in .env")

if not ADMIN_PASSWORD:
    raise SystemExit("ADMIN_PASSWORD is required in .env")

app = FastAPI(title="IoT Control Server")

_sessions: dict[str, float] = {}


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


def get_request_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Session-Token") or request.cookies.get(SESSION_COOKIE_NAME)


def is_session_valid(token: str | None) -> bool:
    if not token:
        return False
    expires_at = _sessions.get(token)
    if not expires_at:
        return False
    if expires_at < time.time():
        _sessions.pop(token, None)
        return False
    return True


def require_admin_session(request: Request):
    token = get_request_token(request)
    if not is_session_valid(token):
        raise HTTPException(status_code=401, detail="Authentication required")
    return True


def create_session_response(payload: dict[str, Any], token: str | None = None) -> JSONResponse:
    session_token = token or secrets.token_urlsafe(32)
    _sessions[session_token] = time.time() + SESSION_TTL_SECONDS
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


def ensure_device_for_device_token(conn, device_name: str, device_token: str, metadata: dict[str, Any] | None = None):
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


@app.post("/api/login")
async def login(request: Request):
    data = await request.json()
    password = str(data.get("password") or "")
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Invalid password")
    return create_session_response({"status": "ok"})


@app.post("/api/logout")
async def logout(request: Request):
    token = get_request_token(request)
    if token:
        _sessions.pop(token, None)
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
        if rows:
            ids = [row["id"] for row in rows]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE commands
                    SET status = 'sent',
                        sent_at = now()
                    WHERE id = ANY(%s::bigint[])
                    """,
                    (ids,),
                )
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


@app.get("/")
async def index():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
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
              <select id="device-select" onchange="refreshAll()"></select>
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
    </div>
  </div>

<script>
  let refreshInterval = null;
  let debounceHandle = null;

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
      throw new Error(error);
    }
    return data;
  }

  function showLogin(message = '') {
    if (refreshInterval) window.clearInterval(refreshInterval);
    if (debounceHandle) window.clearTimeout(debounceHandle);
    refreshInterval = null;
    debounceHandle = null;
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
    const me = await api('/api/me', { method: 'GET' });
    if (!me.authenticated) {
      showLogin('');
      return;
    }
    showDashboard();
    await loadDevices();
    await refreshAll();
    if (refreshInterval) window.clearInterval(refreshInterval);
    refreshInterval = window.setInterval(() => refreshAll().catch(console.error), 5000);
  }

  async function loadDevices() {
    const data = await api('/api/devices', { method: 'GET' });
    const select = document.getElementById('device-select');
    select.innerHTML = '';
    for (const device of data.devices) {
      const option = document.createElement('option');
      option.value = device.device_name;
      option.textContent = device.device_name;
      select.appendChild(option);
    }
    if (!select.value && select.options.length) select.value = select.options[0].value;
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

    document.getElementById('session-info').textContent = JSON.stringify({
      device_name: deviceName,
      metrics_returned: latest.rows?.length || 0,
      recent_rows: recent.rows?.length || 0,
      commands: commands.rows?.length || 0,
    }, null, 2);
  }

  function debouncedRefresh() {
    if (debounceHandle) window.clearTimeout(debounceHandle);
    debounceHandle = window.setTimeout(() => refreshAll().catch(console.error), 350);
  }

  function renderMetrics(rows) {
    const map = {};
    for (const row of rows) map[row.metric] = row.value;
    document.getElementById('kpi-temp').textContent = map.temperatura ?? '--';
    document.getElementById('kpi-hum').textContent = map.umiditate ?? '--';
    document.getElementById('kpi-light').textContent = map.lumina ?? '--';
    const pump = map.pompa;
    const heat = map.incalzire;
    const cool = map.racire;
    document.getElementById('kpi-status').textContent = [pump, heat, cool].every(v => v !== undefined) ? 'online' : 'partial';

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
      document.getElementById('command-result').textContent = err.message;
    }
  }

  bootstrap().catch(err => showLogin(err.message));
</script>
</body>
</html>
        """,
        media_type="text/html",
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "5000"))
    uvicorn.run("server:app", host=host, port=port)
