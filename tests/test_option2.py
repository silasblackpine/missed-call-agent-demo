from unittest.mock import patch

import httpx
import pytest

from app import consent as consent_store
from app import optout
from app.main import PINE_CONSENT_VERSION

from .test_app import signed_post

TWIML = '<?xml version="1.0"?><Response><Connect><Stream url="wss://x"/></Connect></Response>'
CALL = {"From": "+15551230000", "To": "+18556214282", "CallSid": "CAtest"}
SECRET = "test-tool-secret"


@pytest.fixture
def tool_secret(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_TOOL_SECRET", SECRET)


def tool_post(client, payload, secret=SECRET):
    headers = {"X-Tool-Secret": secret} if secret is not None else {}
    return client.post("/api/tools/send-text", json=payload, headers=headers)


def test_flag_off_voice_unchanged(client):
    r = signed_post(client, "/api/voice", {"From": "+15551234567"})
    assert r.status_code == 200
    assert "/api/dial-status" in r.text
    assert "<Connect" not in r.text
    assert "Connecting you now" not in r.text


def test_direct_answer_rings_forward_without_polly(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("FORWARD_TO_NUMBER", "+19095551212")
    monkeypatch.setenv("DIAL_TIMEOUT_SECONDS", "20")
    r = signed_post(client, "/api/voice", CALL)
    assert r.status_code == 200
    assert "<Dial" in r.text and 'action="/api/dial-status"' in r.text
    assert "+19095551212" in r.text
    assert 'timeout="20"' in r.text
    assert "Connecting you now" not in r.text
    assert "<Gather" not in r.text
    assert "<Connect" not in r.text


def test_direct_answer_without_forward_redirects_to_dial_status(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("FORWARD_TO_NUMBER", "")
    r = signed_post(client, "/api/voice", CALL)
    assert r.status_code == 200
    assert "DialCallStatus=no-answer" in r.text
    assert "<Connect" not in r.text
    assert "Connecting you now" not in r.text


@patch("app.main.register_elevenlabs_call", return_value=TWIML)
def test_missed_dial_hands_off_silently_to_pine(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "no-answer"})
    assert r.status_code == 200
    assert r.text == TWIML
    assert "<Say" not in r.text
    assert "<Gather" not in r.text
    assert "/api/consent" not in r.text
    reg.assert_called_once()
    caller, called = reg.call_args.args[:2]
    assert caller == "+15551230000"
    assert called == "+18556214282"
    dynamic = reg.call_args.kwargs["dynamic"]
    assert dynamic["caller_number"] == "+15551230000"
    assert dynamic["call_sid"] == "CAtest"
    assert dynamic["sms_opted_out"] == "false"


@patch("app.main.register_elevenlabs_call", return_value=TWIML)
def test_missed_dial_passes_opted_out(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    optout.handle_keyword("+15551230000", "STOP")
    signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "busy"})
    assert reg.call_args.kwargs["dynamic"]["sms_opted_out"] == "true"


@patch("app.main.register_elevenlabs_call", side_effect=httpx.ConnectTimeout("x"))
def test_missed_dial_fail_safe_with_short_say(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "no-answer"})
    assert r.status_code == 200
    assert "<Connect" not in r.text
    assert "having trouble connecting" in r.text
    assert "<Hangup" in r.text


@patch("app.main.register_elevenlabs_call")
def test_answered_dial_does_not_hand_off(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "completed"})
    assert r.status_code == 200
    assert "<Connect" not in r.text
    assert "<Gather" not in r.text
    assert "<Hangup" in r.text
    reg.assert_not_called()


def test_flag_off_dial_status_still_asks_consent(client, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_DIRECT_ANSWER", raising=False)
    r = signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "no-answer"})
    assert "<Gather" in r.text and 'action="/api/consent"' in r.text
    assert "<Connect" not in r.text


def test_send_text_rejects_missing_or_wrong_secret(client, tool_secret):
    assert tool_post(client, {"caller_number": "+15551230000", "message_type": "booking_link"},
                     secret=None).status_code == 401
    assert tool_post(client, {"caller_number": "+15551230000", "message_type": "booking_link"},
                     secret="wrong").status_code == 401


def test_send_text_rejects_unknown_type(client, tool_secret):
    r = tool_post(client, {"caller_number": "+15551230000", "message_type": "promo"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "reason": "invalid_request"}


@patch("app.main.handle_missed_call")
def test_send_text_stores_consent_then_sends(h, client, tool_secret):
    records = []
    consent_store.set_store(records.append)
    try:
        r = tool_post(client, {
            "caller_number": "+15551230000",
            "message_type": "booking_link",
            "call_sid": "CAtest",
            "consent_answer": "yes",
        })
    finally:
        consent_store.set_store(consent_store.log_store)
    assert r.json() == {"ok": True, "reason": "sent", "message_type": "booking_link"}
    h.assert_called_once_with("+15551230000")
    assert records and records[0]["consented"] is True
    assert records[0]["greeting_version"] == PINE_CONSENT_VERSION
    assert records[0]["answer"] == "pine_tool:booking_link:yes"
    assert records[0]["call_sid"] == "CAtest"


@patch("app.main.handle_missed_call")
def test_send_text_accepts_nested_parameters(h, client, tool_secret):
    r = tool_post(client, {"parameters": {
        "caller_number": "+15551230000",
        "message_type": "booking_link",
    }})
    assert r.json()["ok"] is True
    h.assert_called_once()


@patch("app.main.handle_missed_call")
def test_send_text_respects_opt_out(h, client, tool_secret):
    optout.handle_keyword("+15551230000", "STOP")
    r = tool_post(client, {"caller_number": "+15551230000", "message_type": "booking_link"})
    assert r.json() == {"ok": False, "reason": "opted_out"}
    h.assert_not_called()


@patch("app.main.handle_missed_call")
def test_send_text_skips_sms_if_consent_not_stored(h, client, tool_secret):
    def boom(_record):
        raise RuntimeError("db down")

    consent_store.set_store(boom)
    try:
        r = tool_post(client, {"caller_number": "+15551230000", "message_type": "booking_link"})
    finally:
        consent_store.set_store(consent_store.log_store)
    assert r.json() == {"ok": False, "reason": "consent_store_failed"}
    h.assert_not_called()


@patch("app.main.handle_missed_call", side_effect=RuntimeError("twilio down"))
def test_send_text_send_failure_after_consent(h, client, tool_secret):
    r = tool_post(client, {"caller_number": "+15551230000", "message_type": "booking_link"})
    assert r.json() == {"ok": False, "reason": "send_failed"}
    h.assert_called_once()
