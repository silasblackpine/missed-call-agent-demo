from unittest.mock import patch

import httpx
import pytest

from app import consent as consent_store
from app import optout
from app.main import PINE_CONSENT_VERSION, is_clear_sms_consent

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
    assert 'answerOnBridge="true"' in r.text
    assert 'callerId="+18005550100"' in r.text
    assert 'url="/api/voice/screen?caller=%2B15551230000"' in r.text
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
def test_missed_dial_timeout_redirects_to_retry(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {**CALL, "DialCallStatus": "no-answer"})
    assert r.status_code == 200
    assert "<Connect" not in r.text
    assert "having trouble connecting" not in r.text
    assert "/api/pine-connect?attempt=2" in r.text
    assert "<Hangup" not in r.text


@patch("app.main.register_elevenlabs_call", return_value=TWIML)
def test_pine_connect_retry_returns_twiml_on_success(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/pine-connect?attempt=2", CALL)
    assert r.status_code == 200
    assert r.text == TWIML
    assert reg.call_count == 1
    assert reg.call_args.kwargs["dynamic"]["call_sid"] == "CAtest"


@patch("app.main.register_elevenlabs_call", side_effect=httpx.ReadTimeout("x"))
def test_pine_connect_exhausts_retries_then_fail_safe(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/pine-connect?attempt=3", CALL)
    assert r.status_code == 200
    assert "having trouble connecting" in r.text
    assert "<Hangup" in r.text
    assert "/api/pine-connect" not in r.text
    assert "<Connect" not in r.text


@patch("app.main.register_elevenlabs_call")
def test_answered_dial_does_not_hand_off(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {
        **CALL, "DialCallStatus": "completed", "DialBridged": "true",
    })
    assert r.status_code == 200
    assert "<Connect" not in r.text
    assert "<Gather" not in r.text
    assert "<Hangup" in r.text
    reg.assert_not_called()


@patch("app.main.register_elevenlabs_call", return_value=TWIML)
def test_unbridged_completed_hands_off_to_pine(reg, client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DIRECT_ANSWER", "true")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    r = signed_post(client, "/api/dial-status", {
        **CALL, "DialCallStatus": "completed", "DialBridged": "false",
    })
    assert r.status_code == 200
    assert r.text == TWIML
    assert "<Say" not in r.text
    assert "<Gather" not in r.text
    reg.assert_called_once()


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
        "consent_answer": "yes",
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
        r = tool_post(client, {
            "caller_number": "+15551230000",
            "message_type": "booking_link",
            "consent_answer": "yes",
        })
    finally:
        consent_store.set_store(consent_store.log_store)
    assert r.json() == {"ok": False, "reason": "consent_store_failed"}
    h.assert_not_called()


@patch("app.main.handle_missed_call", side_effect=RuntimeError("twilio down"))
def test_send_text_send_failure_after_consent(h, client, tool_secret):
    r = tool_post(client, {
        "caller_number": "+15551230000",
        "message_type": "booking_link",
        "consent_answer": "yes",
    })
    assert r.json() == {"ok": False, "reason": "send_failed"}
    h.assert_called_once()


def _send_text_capturing(client, payload):
    records = []
    consent_store.set_store(records.append)
    try:
        response = tool_post(client, payload)
    finally:
        consent_store.set_store(consent_store.log_store)
    return response, records



CONSENT_PHRASES = [
    # Accept: required cases
    ("Yeah, that's fine.", True),
    ("Sure, go ahead", True),
    ("Yes please", True),
    ("Okay", True),
    ("Uh huh", True),
    ("Sounds good", True),
    ("Text me", True),
    ("Why not", True),
    # Accept: additional clear affirmatives
    ("yes", True),
    ("yeah sure", True),
    ("sure, text me", True),
    ("Yes!", True),
    ("that's fine", True),
    ("it's fine", True),
    ("yep", True),
    ("yup", True),
    ("alright", True),
    ("all right", True),
    ("go ahead", True),
    ("do it", True),
    ("send it", True),
    ("that works", True),
    ("perfect", True),
    ("absolutely", True),
    ("definitely", True),
    ("of course", True),
    ("Why not?", True),
    ("mhm", True),
    ("uh-huh", True),
    ("please do", True),
    ("works for me", True),
    ("Yeah, go ahead and send it please", True),
    # Refuse: required cases
    ("No", False),
    ("No thanks", False),
    ("Not now", False),
    ("Maybe later", False),
    ("Yeah no", False),
    ("Don't text me", False),
    ("Stop", False),
    ("", False),
    ("Wait what?", False),
    ("I'll call back", False),
    # Refuse: additional negatives, hedges, and empties
    ("yes, don't text me", False),
    ("sure, not now", False),
    ("maybe", False),
    ("I guess", False),
    ("I guess not", False),
    ("hold on", False),
    ("unsubscribe", False),
    ("wait", False),
    ("   ", False),
    ("Can you send it?", False),
]


@pytest.mark.parametrize("quote,expected", CONSENT_PHRASES)
def test_is_clear_sms_consent_phrases(quote, expected):
    assert is_clear_sms_consent(quote) is expected
    assert len(CONSENT_PHRASES) >= 25


@pytest.mark.parametrize("payload", [
    {"caller_number": "+15551230000", "message_type": "booking_link", "call_sid": "CAtest"},
    {"parameters": {"caller_number": "+15551230000", "message_type": "booking_link"}},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": None},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": ""},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "   "},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "\n\t "},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "no"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "no thanks"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "don't text me"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "Don't text me"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "do not text me"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "not now"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "yes, don't text me"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "yeah no"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "maybe"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": "I guess"},
    {"caller_number": "+15551230000", "message_type": "booking_link", "consent_answer": True},
])
@patch("app.main.handle_missed_call")
def test_send_text_refuses_without_clear_yes(h, client, tool_secret, payload):
    response, records = _send_text_capturing(client, payload)
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "consent_required"}
    h.assert_not_called()
    assert records == []


@pytest.mark.parametrize("quote", [
    "yes",
    "yeah sure",
    "yeah",
    "sure, text me",
    "yes please",
    "Yes!",
    "Yeah, that's fine.",
    "that's fine",
    "it's fine",
    "Uh huh",
    "Sounds good",
])
@patch("app.main.handle_missed_call")
def test_send_text_sends_when_quote_is_clear_yes(h, client, tool_secret, quote):
    response, records = _send_text_capturing(client, {
        "caller_number": "+15551230000",
        "message_type": "booking_link",
        "call_sid": "CAtest",
        "consent_answer": quote,
    })
    assert response.status_code == 200
    assert response.json() == {"ok": True, "reason": "sent", "message_type": "booking_link"}
    h.assert_called_once_with("+15551230000")
    assert len(records) == 1
    assert records[0]["consented"] is True
    assert records[0]["answer"] == f"pine_tool:booking_link:{quote}"
    assert records[0]["call_sid"] == "CAtest"
    assert records[0]["caller"] == "+15551230000"
    assert records[0]["greeting_version"] == PINE_CONSENT_VERSION
    assert records[0]["timestamp_utc"]


@patch("app.main.handle_missed_call")
def test_send_text_opted_out_yes_still_blocked(h, client, tool_secret):
    optout.handle_keyword("+15551230000", "STOP")
    response, records = _send_text_capturing(client, {
        "caller_number": "+15551230000",
        "message_type": "booking_link",
        "call_sid": "CAtest",
        "consent_answer": "yes",
    })
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "opted_out"}
    h.assert_not_called()
    assert records == []


def test_register_call_timeout_default(monkeypatch):
    from app import main
    monkeypatch.delenv("ELEVENLABS_TIMEOUT_SECONDS", raising=False)
    assert main.register_call_timeout() == 10.0
    monkeypatch.setenv("ELEVENLABS_TIMEOUT_SECONDS", "12")
    assert main.register_call_timeout() == 12.0
