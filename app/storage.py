"""Durable storage for the opt-out list and consent records.

``SqlStorage`` works with any DB-API connection factory. In production set
``DATABASE_URL`` to a Postgres URL (Supabase, Neon, ...) and
``configure_from_env()`` wires it into ``app.optout`` and ``app.consent``.
Without ``DATABASE_URL`` the in-memory/log defaults stay in place (tests, local).

A new connection is opened per operation, which suits serverless functions;
use the provider's pooled connection string.
"""
import logging
from datetime import datetime, timezone
from typing import Callable

from .config import env

log = logging.getLogger("missed_call.storage")

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sms_opt_outs (
    phone TEXT PRIMARY KEY, blocked BOOLEAN NOT NULL,
    last_keyword TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sms_consent (
    id INTEGER PRIMARY KEY AUTOINCREMENT, caller TEXT NOT NULL,
    call_sid TEXT NOT NULL DEFAULT '', answer TEXT NOT NULL DEFAULT '',
    consented BOOLEAN NOT NULL, greeting_version TEXT NOT NULL DEFAULT '',
    timestamp_utc TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqlStorage:
    """Opt-out store (block/unblock/is_blocked) and consent store (callable)."""

    def __init__(self, connect: Callable, placeholder: str = "%s"):
        self._connect = connect
        self._p = placeholder

    def _exec(self, sql: str, params: tuple = (), fetch: bool = False):
        sql = sql.replace("?", self._p)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone() if fetch else None
            conn.commit()
            return row
        finally:
            conn.close()

    def _set(self, phone: str, blocked: bool, keyword: str) -> None:
        self._exec(
            "INSERT INTO sms_opt_outs (phone, blocked, last_keyword, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (phone) DO UPDATE SET "
            "blocked = EXCLUDED.blocked, last_keyword = EXCLUDED.last_keyword, "
            "updated_at = EXCLUDED.updated_at",
            (phone, blocked, keyword, _now()),
        )

    # opt-out store interface
    def block(self, phone: str, keyword: str = "STOP") -> None:
        self._set(phone, True, keyword)

    def unblock(self, phone: str, keyword: str = "START") -> None:
        self._set(phone, False, keyword)

    def is_blocked(self, phone: str) -> bool:
        row = self._exec("SELECT blocked FROM sms_opt_outs WHERE phone = ?", (phone,), fetch=True)
        return bool(row and row[0])

    # consent store interface
    def __call__(self, record: dict) -> None:
        self._exec(
            "INSERT INTO sms_consent (caller, call_sid, answer, consented, greeting_version, "
            "timestamp_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (record["caller"], record.get("call_sid", ""), record.get("answer", ""),
             bool(record["consented"]), record.get("greeting_version", ""),
             record["timestamp_utc"]),
        )


def postgres_storage(url: str) -> SqlStorage:
    import psycopg  # imported lazily so tests/local need no driver

    return SqlStorage(lambda: psycopg.connect(url, connect_timeout=5), placeholder="%s")


class MissingDatabaseStore:
    """Installed in production when DATABASE_URL is unset: every read/write fails,
    so the opt-out check fails closed and consent writes fail (no texts sent)."""

    def _fail(self, *_a, **_k):
        raise RuntimeError("DATABASE_URL is required in production")

    block = unblock = is_blocked = __call__ = _fail


def is_production() -> bool:
    return "production" in {env("VERCEL_ENV").lower(), env("APP_ENV").lower()}


def configure_from_env():
    url = env("DATABASE_URL")
    from . import consent, optout

    if not url:
        if is_production():
            log.error("DATABASE_URL is not set in production: SMS sending is disabled "
                      "(fail closed) until durable storage is configured")
            store = MissingDatabaseStore()
            optout.set_store(store)
            consent.set_store(store)
            return store
        return None

    store = postgres_storage(url)
    optout.set_store(store)
    consent.set_store(store)
    log.info("durable Postgres storage enabled")
    return store
