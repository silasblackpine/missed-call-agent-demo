import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from app import consent, optout, storage
from tests.test_app import signed_post

N = "+15551234567"


@pytest.fixture
def sql(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(storage.SQLITE_SCHEMA)
    conn.close()
    s = storage.SqlStorage(lambda: sqlite3.connect(db), placeholder="?")
    optout.set_store(s)
    consent.set_store(s)
    yield s, db
    consent.set_store(consent.log_store)


def _rows(db, q):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(q).fetchall()
    finally:
        conn.close()


def test_block_unblock_roundtrip(sql):
    s, db = sql
    assert not s.is_blocked(N)
    s.block(N, "STOP")
    assert s.is_blocked(N)
    s.unblock(N, "UNSTOP")
    assert not s.is_blocked(N)
    assert _rows(db, "SELECT phone, blocked, last_keyword FROM sms_opt_outs") == [(N, 0, "UNSTOP")]


def test_optout_survives_new_store_instance(sql):
    s, db = sql
    s.block(N)
    fresh = storage.SqlStorage(lambda: sqlite3.connect(db), placeholder="?")
    assert fresh.is_blocked(N)


@patch("app.integrations.get_twilio_client")
def test_end_to_end_with_sql_store(mc, client, sql):
    s, db = sql
    tw = MagicMock()
    tw.messages.create.return_value.sid = "SM1"
    mc.return_value = tw
    signed_post(client, "/api/sms", {"From": N, "Body": "Stop"})
    signed_post(client, "/api/consent", {"From": N, "Digits": "1", "CallSid": "CA1"})
    tw.messages.create.assert_not_called()
    signed_post(client, "/api/sms", {"From": N, "Body": "START"})
    signed_post(client, "/api/consent", {"From": N, "Digits": "1", "CallSid": "CA2"})
    tw.messages.create.assert_called_once()
    rows = _rows(db, "SELECT caller, call_sid, consented, greeting_version FROM sms_consent ORDER BY id")
    assert [r[1] for r in rows] == ["CA1", "CA2"]
    assert all(r[2] == 1 and r[3] for r in rows)


@patch("app.integrations.get_twilio_client")
def test_store_failure_fails_closed(mc, client):
    broken = MagicMock()
    broken.is_blocked.side_effect = RuntimeError("db down")
    optout.set_store(broken)
    signed_post(client, "/api/consent", {"From": N, "Digits": "1"})
    mc.assert_not_called()


def test_postgres_placeholder_rewrite():
    cur = MagicMock()
    cur.fetchone.return_value = (True,)
    conn = MagicMock()
    conn.cursor.return_value = cur
    s = storage.SqlStorage(lambda: conn, placeholder="%s")
    assert s.is_blocked(N)
    sql, params = cur.execute.call_args.args
    assert "%s" in sql and "?" not in sql and params == (N,)
    conn.close.assert_called_once()


def test_configure_from_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert storage.configure_from_env() is None
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    with patch("app.storage.postgres_storage") as pg:
        try:
            assert storage.configure_from_env() is pg.return_value
            assert optout._store is pg.return_value and consent._store is pg.return_value
        finally:
            consent.set_store(consent.log_store)


def test_migration_file_creates_tables():
    sql = open("migrations/001_init.sql").read()
    assert "CREATE TABLE IF NOT EXISTS sms_opt_outs" in sql
    assert "CREATE TABLE IF NOT EXISTS sms_consent" in sql
