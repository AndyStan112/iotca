-- Create recurring jobs table for DB-backed scheduled command generation
BEGIN;

CREATE TABLE IF NOT EXISTS recurring_jobs (
    id BIGSERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    job_name TEXT NOT NULL,
    command TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    interval_seconds INTEGER NOT NULL CHECK (interval_seconds > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recurring_jobs_device_active_next_idx
    ON recurring_jobs (device_id, active, next_run_at);

CREATE INDEX IF NOT EXISTS recurring_jobs_device_created_idx
    ON recurring_jobs (device_id, created_at DESC);

COMMIT;
