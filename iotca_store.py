from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any

import psycopg2
import psycopg2.extras

ALLOWED_DEVICE_NAMES = {"greenhouse-01"}
ASSISTANT_ALLOWED_COMMANDS = {"pompa", "racire"}


def connect(database_url: str):
    return psycopg2.connect(
        database_url,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@contextmanager
def db_session(database_url: str):
    conn = connect(database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_device_by_name(conn, device_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, device_name, device_key, metadata FROM devices WHERE device_name = %s",
            (device_name,),
        )
        return cur.fetchone()


def ensure_allowed_device_name(device_name: str):
    if device_name not in ALLOWED_DEVICE_NAMES:
        raise ValueError("Device not allowed")


def get_recent_measurements(conn, device_name: str, limit: int = 500, metric: str | None = None):
    ensure_allowed_device_name(device_name)
    device = load_device_by_name(conn, device_name)
    if not device:
        return []

    limit = max(1, min(int(limit), 1000))
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
    rows.reverse()
    return rows


def summarize_recent_measurements(rows: list[dict[str, Any]], window_points: int = 10):
    window_points = max(1, int(window_points))
    grouped: dict[str, list[float]] = {}
    for row in rows:
        metric_name = row.get("metric")
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        grouped.setdefault(metric_name, []).append(value)

    summary: dict[str, dict[str, Any]] = {}
    for metric_name, values in grouped.items():
        if not values:
            continue
        window_values = values[-window_points:]
        summary[metric_name] = {
            "count": len(window_values),
            "mean": mean(window_values),
            "stddev": pstdev(window_values) if len(window_values) > 1 else 0.0,
            "min": min(window_values),
            "max": max(window_values),
            "latest": window_values[-1],
        }
    return summary


def get_measurement_window(conn, device_name: str, minutes: int = 60, metric: str | None = None):
    ensure_allowed_device_name(device_name)
    device = load_device_by_name(conn, device_name)
    if not device:
        return {"device_name": device_name, "rows": [], "summary": {}}

    minutes = max(1, min(int(minutes), 7 * 24 * 60))
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with conn.cursor() as cur:
        if metric:
            cur.execute(
                """
                SELECT recorded_at, metric, value, payload
                FROM measurements
                WHERE device_id = %s AND metric = %s AND recorded_at >= %s
                ORDER BY recorded_at ASC, id ASC
                """,
                (device["id"], metric, since),
            )
        else:
            cur.execute(
                """
                SELECT recorded_at, metric, value, payload
                FROM measurements
                WHERE device_id = %s AND recorded_at >= %s
                ORDER BY recorded_at ASC, id ASC
                """,
                (device["id"], since),
            )
        rows = cur.fetchall()

    grouped: dict[str, list[float]] = {}
    for row in rows:
        metric_name = row["metric"]
        try:
            value = float(row["value"])
        except (TypeError, ValueError):
            continue
        grouped.setdefault(metric_name, []).append(value)

    summary: dict[str, dict[str, Any]] = {}
    for metric_name, values in grouped.items():
        if not values:
            continue
        summary[metric_name] = {
            "count": len(values),
            "mean": mean(values),
            "stddev": pstdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
            "latest": values[-1],
        }

    return {
        "device_name": device_name,
        "minutes": minutes,
        "rows": rows,
        "summary": summary,
    }


def queue_command(conn, device_name: str, command: str, parameters: dict[str, Any] | None = None):
    ensure_allowed_device_name(device_name)
    if command not in ASSISTANT_ALLOWED_COMMANDS:
        raise ValueError("Command not allowed")

    device = load_device_by_name(conn, device_name)
    if not device:
        raise LookupError("Device not found")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO commands (device_id, command, parameters, status)
            VALUES (%s, %s, %s, 'pending')
            RETURNING id, command, parameters, status, created_at
            """,
            (device["id"], command, psycopg2.extras.Json(parameters or {})),
        )
        return cur.fetchone()
