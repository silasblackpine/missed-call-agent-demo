from unittest.mock import patch

import httpx
import pytest

from app import optout

TWIML = '<?xml version="1.0"?><Response><Connect><Stream url="wss://x"/></Connect></Response>'


@pytest.fixture(autouse=True)
def skip_sig(monkeypatch):
    monkeypatch.setenv("SKIP_TWILIO_VALIDATION", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")


def consent(client, digits):
    return client.post("/api/consent", data={"From": "+15551230000", "To": "+18556214282",
                                             "CallSid": "CA1", "Digits": digits})


@patch("app.main.handle_missed_call")
def test_flag_off_unchanged(h, client, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_HANDOFF_ENABLED", raising=False)
    r = consent(client, "1")
    assert "/api/handoff" not in r.text and "<Hangup" in r.text
    h.assert_called_once()


@patch("app.main.handle_missed_call")
def test_flag_on_texts_then_redirects(h, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    r = consent(client, "1")
    h.assert_called_once()
    assert r.text.index("Thanks. We will attempt") < r.text.index("/api/handoff?after=consented")


@patch("app.main.handle_missed_call")
def test_flag_on_stop_still_blocks_text(h, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    optout.handle_keyword("+15551230000", "STOP")
    r = consent(client, "1")
    h.assert_not_called()
    assert "/api/handoff" in r.text


def test_decline_handoff_needs_second_flag(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    assert "/api/handoff" not in consent(client, "2").text
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ON_DECLINE", "true")
    assert "after=declined" in consent(client, "2").text


def test_decline_never_hands_off_unless_on_decline(client, monkeypatch):
    """A decline hangs up unless ELEVENLABS_HANDOFF_ON_DECLINE is on (with the main flag)."""
    for env in (
        {},
        {"ELEVENLABS_HANDOFF_ENABLED": "true"},
        {"ELEVENLABS_HANDOFF_ON_DECLINE": "true"},
    ):
        monkeypatch.delenv("ELEVENLABS_HANDOFF_ENABLED", raising=False)
        monkeypatch.delenv("ELEVENLABS_HANDOFF_ON_DECLINE", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        r = consent(client, "2")
        assert r.status_code == 200
        assert "/api/handoff" not in r.text
        assert "won't send a text" in r.text and "<Hangup" in r.text

    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ON_DECLINE", "true")
    r = consent(client, "2")
    assert r.status_code == 200 and "after=declined" in r.text


@patch("app.main.handle_missed_call", side_effect=RuntimeError("toll-free not yet verified"))
def test_sms_failure_does_not_block_handoff(h, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    from app import consent as consent_store

    records = []
    consent_store.set_store(records.append)
    try:
        r = consent(client, "1")
    finally:
        consent_store.set_store(consent_store.log_store)
    assert r.status_code == 200
    assert "/api/handoff" in r.text
    assert "Thanks. We will attempt" in r.text
    assert records and records[0]["consented"] is True
    h.assert_called_once()


@patch("app.main.register_elevenlabs_call", return_value=TWIML)
def test_handoff_returns_elevenlabs_twiml(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    r = client.post("/api/handoff?after=consented", data={"From": "+15551230000", "To": "+18556214282"})
    assert r.text == TWIML
    reg.assert_called_once_with("+15551230000", "+18556214282")


@patch("app.main.register_elevenlabs_call", side_effect=httpx.ConnectTimeout("x"))
def test_handoff_error_fails_safe(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_HANDOFF_ENABLED", "true")
    r = client.post("/api/handoff?after=declined", data={"From": "+15551230000", "To": "+18556214282"})
    assert "won't send a text" in r.text and "<Hangup" in r.text
    r = client.post("/api/handoff?after=consented", data={"From": "+15551230000", "To": "+18556214282"})
    assert "<Hangup" in r.text and "<Connect" not in r.text
