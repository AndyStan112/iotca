-- Create tables for IoT time-series data
BEGIN;

-- Devices table: one row per physical device (e.g., a Raspberry Pi)
CREATE TABLE IF NOT EXISTS devices (
    id SERIAL PRIMARY KEY,
    device_name TEXT UNIQUE NOT NULL,
    device_key TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Measurements table: time-series rows for graphing and analysis
CREATE TABLE IF NOT EXISTS measurements (
    id BIGSERIAL PRIMARY KEY,
    device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ NOT NULL,
    metric TEXT,
    value DOUBLE PRECISION,
    payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common queries: recent points per device, time-range scans and metric lookups
CREATE INDEX IF NOT EXISTS measurements_device_recorded_idx ON measurements (device_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS measurements_recorded_idx ON measurements (recorded_at DESC);
CREATE INDEX IF NOT EXISTS measurements_metric_recorded_idx ON measurements (metric, recorded_at DESC);

COMMIT;
