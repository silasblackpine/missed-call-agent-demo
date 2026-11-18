from unittest.mock import MagicMock, patch

from app import consent
from app.main import CONFIRMATION, GREETING, GREETING_VERSION, HELP_REPLY
from tests.test_app import signed_post

N = "+15551234567"


def _tw(mc):
    tw = MagicMock()
    tw.messages.create.return_value.sid = "SM1"
    mc.return_value = tw
    return tw


def test_greeting_and_confirmation_match_final_page():
    assert GREETING.startswith("Hi, you've reached Black Pine Lab.")
    assert "This call may be recorded." in GREETING
    assert "Press 1 or say yes to get one booking-link text after your consent." in GREETING
    assert "Message and data rates may apply." in GREETING
    assert "Reply HELP for help or STOP to opt out anytime." in GREETING
    assert "blackpinelab.com/terms" in GREETING and "blackpinelab.com/privacy" in GREETING
    assert GREETING.endswith("Hang up to skip.")
    assert GREETING_VERSION == "2026-11-18-a1"
    assert CONFIRMATION.startswith("Thanks. If your number is eligible")
    assert "Text START to this number first" in CONFIRMATION
    assert "Goodbye." not in CONFIRMATION


@patch("app.integrations.get_twilio_client")
def test_consent_yes_sends(mc, client):
    tw = _tw(mc)
    signed_post(client, "/api/consent", {"From": N, "Digits": "1", "CallSid": "CA1"})
    tw.messages.create.assert_called_once()


@patch("app.integrations.get_twilio_client")
def test_silence_sends_nothing(mc, client):
    r = signed_post(client, "/api/consent", {"From": N, "CallSid": "CA2"})
    assert "won't send a text" in r.text
    mc.assert_not_called()


@patch("app.integrations.get_twilio_client")
def test_stop_blocks_later_consent(mc, client):
    tw = _tw(mc)
    for kw in ("STOP", "stopall", "Unsubscribe", "CANCEL", "end", "QUIT"):
        from app import optout
        optout.reset()
        r = signed_post(client, "/api/sms", {"From": N, "Body": kw})
        assert "<Message" not in r.text  # carrier sends STOP confirmation
        r = signed_post(client, "/api/consent", {"From": N, "SpeechResult": "yes"})
        assert "Text START to this number first" in r.text
    tw.messages.create.assert_not_called()


@patch("app.integrations.get_twilio_client")
def test_start_and_unstop_lift_block(mc, client):
    tw = _tw(mc)
    for kw in ("START", "unstop"):
        signed_post(client, "/api/sms", {"From": N, "Body": "STOP"})
        r = signed_post(client, "/api/sms", {"From": N, "Body": kw})
        assert "<Message" not in r.text
        signed_post(client, "/api/consent", {"From": N, "Digits": "1"})
    assert tw.messages.create.call_count == 2


def test_help_reply_text(client):
    r = signed_post(client, "/api/sms", {"From": N, "Body": "help"})
    assert ("<Message>Black Pine Lab: missed-call follow-up texts. Email "
            "support@blackpinelab.com. Reply STOP to opt out.</Message>") in r.text
    assert HELP_REPLY in r.text


def test_sms_rejects_bad_signature(client):
    r = client.post("/api/sms", data={"From": N, "Body": "STOP"},
                    headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


@patch("app.integrations.get_twilio_client")
def test_greeting_version_logged(mc, client):
    _tw(mc)
    records = []
    consent.set_store(records.append)
    try:
        signed_post(client, "/api/consent", {"From": N, "Digits": "1"})
    finally:
        consent.set_store(consent.log_store)
    assert records[0]["greeting_version"] == GREETING_VERSION
