-- Create commands table for remote actuator control and polling
BEGIN;

CREATE TABLE IF NOT EXISTS commands (
    id BIGSERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    result JSONB
);

CREATE INDEX IF NOT EXISTS commands_device_status_idx ON commands (device_id, status);

COMMIT;
