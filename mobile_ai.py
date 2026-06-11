from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.media import Image
from agno.models.openai import OpenAIResponses
from pydantic import BaseModel, Field

import psycopg2
import psycopg2.extras

from iotca_store import ASSISTANT_ALLOWED_COMMANDS, db_session, get_measurement_window, queue_command


class PlantAnalysis(BaseModel):
    plant_looks_healthy: bool = Field(..., description="True when the plant appears generally healthy.")
    health_status: Literal["healthy", "watch", "unhealthy"] = Field(
        ..., description="Short health label for the UI."
    )
    summary: str = Field(..., description="One short paragraph suitable for a card in the UI.")
    observations: list[str] = Field(default_factory=list, description="Visible observations from the image and stats.")
    suggestions: list[str] = Field(default_factory=list, description="Actionable suggestions for the grower.")
    concerns: list[str] = Field(default_factory=list, description="Anything that looks wrong or needs attention.")
    confidence: int = Field(..., ge=0, le=100, description="Confidence from 0 to 100.")


def _read_api_key() -> str | None:
    return os.getenv("OPENAI_API_KEY") or None


@lru_cache(maxsize=1)
def _agent_db() -> SqliteDb:
    db_file = os.getenv("MOBILE_AGENT_DB_FILE", "/tmp/iotca-mobile-agent.db")
    return SqliteDb(db_file=db_file)


def _format_metric_value(metric: str, value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)

    if metric == "temperatura":
        return f"{numeric:.1f} C"
    if metric == "umiditate":
        return f"{numeric:.1f}%"
    if metric == "lumina":
        brightness = max(0.0, min(100.0, ((255.0 - numeric) / 255.0) * 100.0))
        return f"{brightness:.0f}% brightness (raw {numeric:.0f}/255, 255 = pitch black)"
    return str(value)


def build_mobile_context_text(
    window_summary: dict[str, Any],
    latest_rows: list[dict[str, Any]],
    recurring_jobs: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        "Greenhouse context:",
        "Temperature is in Celsius.",
        "Humidity is a percentage.",
        "Light uses a reversed 0-255 raw scale where 255 means pitch black.",
        "",
        f"Window length: {window_summary.get('window_points', 'unknown')} points.",
    ]

    summary = window_summary.get("summary") or {}
    if summary:
        lines.append("Window statistics:")
        for metric in ("temperatura", "umiditate", "lumina"):
            metric_summary = summary.get(metric)
            if not metric_summary:
                continue
            lines.append(
                f"- {metric}: mean {_format_metric_value(metric, metric_summary.get('mean'))}, "
                f"std dev {metric_summary.get('stddev'):.2f}, latest {_format_metric_value(metric, metric_summary.get('latest'))}"
            )
    else:
        lines.append("Window statistics: no recent measurements available.")

    if latest_rows:
        lines.append("")
        lines.append("Latest measurements:")
        for row in latest_rows[-6:]:
            lines.append(
                f"- {row.get('metric')}: {_format_metric_value(row.get('metric', ''), row.get('value'))} at {row.get('recorded_at')}"
            )

    recurring_jobs = recurring_jobs or []
    if recurring_jobs:
        lines.append("")
        lines.append("Scheduled jobs:")
        for job in recurring_jobs[:10]:
            status = "active" if job.get("active") else "paused"
            next_run = job.get("next_run_at") or "unknown"
            last_run = job.get("last_run_at") or "never"
            lines.append(
                f"- #{job.get('id')} {job.get('job_name')} -> {job.get('command')} every {job.get('interval_seconds')}s "
                f"({status}, next {next_run}, last {last_run})"
            )

    return "\n".join(lines)


def build_mobile_agent(database_url: str, device_name: str) -> Agent:
    def load_device(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT id, device_name, metadata FROM devices WHERE device_name = %s", (device_name,))
            return cur.fetchone()

    def _rows_as_dicts(rows):
        return [dict(row) for row in rows]

    def query_history(metric: str | None = None, minutes: int = 60, limit: int | None = None):
        """Query older measurement history for the selected device."""

        with db_session(database_url) as conn:
            window = get_measurement_window(conn, device_name=device_name, minutes=minutes, metric=metric)
            rows = window["rows"]
            if limit is not None:
                rows = rows[-max(1, min(int(limit), 250)) :]
            return {
                "device_name": device_name,
                "minutes": window["minutes"],
                "metric": metric,
                "summary": window["summary"],
                "rows": rows,
            }

    def queue_actuator(command: str, state: str, duration_seconds: int | None = None):
        """Queue a non-heater actuator command for the Pi exporter to execute."""

        if command not in ASSISTANT_ALLOWED_COMMANDS:
            return {"status": "error", "message": "That actuator is disabled in the assistant."}
        parameters: dict[str, Any] = {"state": state}
        if duration_seconds is not None:
            parameters["duration_seconds"] = max(1, int(duration_seconds))

        with db_session(database_url) as conn:
            row = queue_command(conn, device_name=device_name, command=command, parameters=parameters)
            return {
                "status": "queued",
                "command_id": row["id"],
                "command": row["command"],
                "parameters": row["parameters"],
            }

    def query_recurring_jobs(limit: int = 25):
        """Read recurring jobs for the selected device."""

        with db_session(database_url) as conn:
            device = load_device(conn)
            if not device:
                return {"status": "error", "message": "Device not found"}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, job_name, command, parameters, interval_seconds, active, next_run_at, last_run_at, created_at, updated_at
                    FROM recurring_jobs
                    WHERE device_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (device["id"], max(1, min(int(limit), 100))),
                )
                rows = cur.fetchall()
            return {"device_name": device_name, "rows": _rows_as_dicts(rows)}

    def create_recurring_job(
        job_name: str,
        command: str,
        interval_seconds: int,
        state: str = "on",
        duration_seconds: int | None = None,
        active: bool = True,
    ):
        """Create a recurring job for the selected device."""

        if command not in ASSISTANT_ALLOWED_COMMANDS:
            return {"status": "error", "message": "That actuator is disabled in the assistant."}
        parameters: dict[str, Any] = {"state": state}
        if duration_seconds is not None:
            parameters["duration_seconds"] = max(1, int(duration_seconds))

        with db_session(database_url) as conn:
            device = load_device(conn)
            if not device:
                return {"status": "error", "message": "Device not found"}
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
                        device["id"],
                        job_name,
                        command,
                        psycopg2.extras.Json(parameters),
                        max(1, int(interval_seconds)),
                        active,
                        active,
                        max(1, int(interval_seconds)),
                    ),
                )
                row = cur.fetchone()
            return {"status": "created", "job": dict(row) if row else None}

    def toggle_recurring_job(job_id: int, active: bool):
        """Enable or pause an existing recurring job."""

        with db_session(database_url) as conn:
            device = load_device(conn)
            if not device:
                return {"status": "error", "message": "Device not found"}
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
                    (active, active, int(job_id), device["id"]),
                )
                row = cur.fetchone()
            if not row:
                return {"status": "error", "message": "Recurring job not found"}
            return {"status": "ok", "job": dict(row)}

    description = (
        "You are a greenhouse assistant for a Raspberry Pi sensor node. "
        "The device reports temperature in Celsius, humidity as a percentage, and light on a reversed 0-255 raw scale "
        "where 255 means pitch black. The assistant can talk naturally, inspect the current image, and optionally query "
        "older data or queue safe actuator commands. Heater control is intentionally unavailable because it is too dangerous."
    )

    instructions = [
        "Answer chat questions in plain text. Keep it concise and useful.",
        "When you need more context, use the history query tool rather than inventing numbers.",
        "When acting, only use pump or cooler commands. Never try to control a heater.",
        "When analyzing an image, focus on visible plant health, moisture clues, discoloration, drooping, stress, and trends in the measurements.",
        "Remember that light is inverted: higher raw values mean darker conditions.",
        "You can inspect recurring jobs and create or pause them when asked.",
    ]

    return Agent(
        model=OpenAIResponses(id="gpt-5-mini", api_key=_read_api_key()),
        db=_agent_db(),
        name="mobile-greenhouse-assistant",
        description=description,
        instructions=instructions,
        tools=[query_history, queue_actuator, query_recurring_jobs, create_recurring_job, toggle_recurring_job],
        tool_call_limit=4,
        markdown=False,
        add_datetime_to_context=True,
        timezone_identifier="Europe/Bucharest",
        add_history_to_context=True,
        num_history_runs=3,
    )


def _extract_content_text(result: Any) -> str:
    content = getattr(result, "content", result)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if hasattr(content, "model_dump_json"):
        return content.model_dump_json(exclude_none=True)
    if hasattr(content, "model_dump"):
        return json.dumps(content.model_dump(exclude_none=True), ensure_ascii=False)
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def coerce_analysis_payload(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", result)
    if content is None:
        return {
            "plant_looks_healthy": False,
            "health_status": "watch",
            "summary": "No analysis was returned.",
            "observations": [],
            "suggestions": [],
            "concerns": ["The model did not return structured output."],
            "confidence": 0,
        }
    if isinstance(content, BaseModel):
        return content.model_dump()
    if isinstance(content, dict):
        return content
    if hasattr(content, "model_dump"):
        return content.model_dump()
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {
                "plant_looks_healthy": False,
                "health_status": "watch",
                "summary": content,
                "observations": [],
                "suggestions": [],
                "concerns": [],
                "confidence": 0,
            }
        if isinstance(parsed, dict):
            return parsed
    return {
        "plant_looks_healthy": False,
        "health_status": "watch",
        "summary": str(content),
        "observations": [],
        "suggestions": [],
        "concerns": [],
        "confidence": 0,
    }


async def run_chat(
    *,
    database_url: str,
    device_name: str,
    message: str,
    window_summary: dict[str, Any],
    latest_rows: list[dict[str, Any]],
    snapshot_path: Path | None,
    session_id: str | None,
):
    agent = build_mobile_agent(database_url=database_url, device_name=device_name)
    prompt = "\n\n".join(
        [
            build_mobile_context_text(window_summary, latest_rows, window_summary.get("recurring_jobs") or []),
            "User message:",
            message.strip(),
        ]
    ).strip()

    images = [Image(filepath=snapshot_path)] if snapshot_path and snapshot_path.exists() else None
    result = await agent.arun(
        prompt,
        session_id=session_id,
        images=images,
    )
    return _extract_content_text(result)


async def run_analysis(
    *,
    database_url: str,
    device_name: str,
    message: str,
    window_summary: dict[str, Any],
    latest_rows: list[dict[str, Any]],
    snapshot_path: Path | None,
    session_id: str | None,
):
    agent = build_mobile_agent(database_url=database_url, device_name=device_name)
    prompt = "\n\n".join(
        [
            "Perform a plant health analysis for the greenhouse.",
            "Return structured output only.",
            build_mobile_context_text(window_summary, latest_rows, window_summary.get("recurring_jobs") or []),
            "User request:",
            message.strip() or "Analyze the plant health, image, and the recent measurement window.",
        ]
    ).strip()

    images = [Image(filepath=snapshot_path)] if snapshot_path and snapshot_path.exists() else None
    result = await agent.arun(
        prompt,
        session_id=session_id,
        images=images,
        output_schema=PlantAnalysis,
    )
    return coerce_analysis_payload(result)
