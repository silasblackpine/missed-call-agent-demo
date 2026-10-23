-- Durable storage for missed-call-agent-demo (Postgres: Supabase, Neon, etc.)
-- Run once in the database's SQL editor (or: psql "$DATABASE_URL" -f migrations/001_init.sql).
-- Safe to re-run.

CREATE TABLE IF NOT EXISTS sms_opt_outs (
    phone        TEXT PRIMARY KEY,          -- E.164, e.g. +15551234567
    blocked      BOOLEAN NOT NULL,          -- true after STOP-type keyword, false after START/UNSTOP
    last_keyword TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_consent (
    id               BIGSERIAL PRIMARY KEY,
    caller           TEXT NOT NULL,
    call_sid         TEXT NOT NULL DEFAULT '',
    answer           TEXT NOT NULL DEFAULT '',
    consented        BOOLEAN NOT NULL,
    greeting_version TEXT NOT NULL DEFAULT '',
    timestamp_utc    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS sms_consent_caller_idx ON sms_consent (caller, timestamp_utc DESC);
