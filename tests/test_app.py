from unittest.mock import MagicMock, patch

from twilio.request_validator import RequestValidator

from app import integrations

BASE = "https://demo.example.com"


def signed_post(client, path, data):
    sig = RequestValidator("test_token").compute_signature(BASE + path, data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": sig})


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_rejects_bad_signature(client):
    r = client.post("/api/voice", data={"From": "+15551234567"},
                    headers={"X-Twilio-Signature": "bogus"})
    assert r.status_code == 403


def test_voice_dials_forward_number(client, monkeypatch):
    monkeypatch.setenv("FORWARD_TO_NUMBER", "+15550001111")
    r = signed_post(client, "/api/voice", {"From": "+15551234567"})
    assert r.status_code == 200
    assert "<Dial" in r.text and "+15550001111" in r.text
    assert 'action="/api/dial-status"' in r.text


def test_voice_without_forward_redirects_to_missed(client):
    r = signed_post(client, "/api/voice", {"From": "+15551234567"})
    assert "<Redirect" in r.text


@patch("app.integrations.get_twilio_client")
def test_missed_call_prompts_for_consent_without_texting(mock_client, client):
    r = signed_post(client, "/api/dial-status",
                    {"From": "+15551234567", "DialCallStatus": "no-answer"})
    assert r.status_code == 200
    assert "<Gather" in r.text and 'action="/api/consent"' in r.text
    assert 'input="dtmf speech"' in r.text and 'numDigits="1"' in r.text
    assert "Press 1 or say yes to receive the text." in r.text
    mock_client.assert_not_called()


def _sms_mock(mock_client):
    tw = MagicMock()
    tw.messages.create.return_value.sid = "SM123"
    mock_client.return_value = tw
    return tw


@patch("app.integrations.get_twilio_client")
def test_consent_digit_1_sends_sms(mock_client, client):
    tw = _sms_mock(mock_client)
    r = signed_post(client, "/api/consent",
                    {"From": "+15551234567", "Digits": "1", "CallSid": "CA1"})
    assert "We will attempt to send your Black Pine Lab booking link" in r.text
    kwargs = tw.messages.create.call_args.kwargs
    assert kwargs["to"] == "+15551234567"
    assert kwargs["body"] == ("Hi, this is Black Pine Lab, sorry we missed your call! "
                              "You can book a time here: https://calendly.com/demo/intro "
                              "or reply with how we can help. Reply STOP to opt out.")


@patch("app.integrations.get_twilio_client")
def test_consent_speech_yes_sends_sms(mock_client, client):
    tw = _sms_mock(mock_client)
    signed_post(client, "/api/consent",
                {"From": "+15551234567", "SpeechResult": "Yes, please.", "CallSid": "CA2"})
    tw.messages.create.assert_called_once()


@patch("app.integrations.get_twilio_client")
def test_other_input_sends_nothing(mock_client, client):
    for data in ({"Digits": "2"}, {"SpeechResult": "no thanks"}, {"SpeechResult": "yesterday"}, {}):
        r = signed_post(client, "/api/consent", {"From": "+15551234567", **data})
        assert "won't send a text" in r.text
    mock_client.assert_not_called()


def test_consent_record_logged(client):
    from app import consent
    records = []
    consent.set_store(records.append)
    try:
        with patch("app.integrations.get_twilio_client") as mc:
            _sms_mock(mc)
            signed_post(client, "/api/consent",
                        {"From": "+15551234567", "Digits": "1", "CallSid": "CA9"})
            signed_post(client, "/api/consent", {"From": "+15551234567", "CallSid": "CA10"})
    finally:
        consent.set_store(consent.log_store)
    assert records[0]["caller"] == "+15551234567" and records[0]["call_sid"] == "CA9"
    assert records[0]["answer"] == "digits:1" and records[0]["consented"] is True
    assert records[0]["timestamp_utc"].endswith("+00:00")
    assert records[1]["consented"] is False


@patch("app.integrations.get_twilio_client")
def test_answered_call_sends_nothing(mock_client, client):
    signed_post(client, "/api/dial-status",
                {"From": "+15551234567", "DialCallStatus": "completed"})
    mock_client.assert_not_called()


def test_hooks_disabled_by_default():
    assert integrations.create_hubspot_lead("+1555") is None
    assert integrations.send_resend_notification("+1555") is None


def _resp(payload):
    m = MagicMock()
    m.json.return_value = payload
    return m


def test_hubspot_creates_contact_and_deal(monkeypatch):
    monkeypatch.setenv("ENABLE_HUBSPOT", "true")
    monkeypatch.setenv("HUBSPOT_PIPELINE_STAGE_ID", "stage1")
    with patch("app.integrations.httpx.Client") as C:
        c = C.return_value.__enter__.return_value
        c.post.side_effect = [_resp({"id": "c1"}), _resp({"id": "d1"})]
        assert integrations.create_hubspot_lead("+1555") == {"contact_id": "c1", "deal_id": "d1"}
        deal_json = c.post.call_args_list[1].kwargs["json"]
        assert deal_json["properties"]["dealstage"] == "stage1"


def test_resend_sends_from_support(monkeypatch):
    monkeypatch.setenv("ENABLE_RESEND", "true")
    monkeypatch.setenv("NOTIFY_EMAIL", "owner@example.com")
    with patch("app.integrations.httpx.post", return_value=_resp({"id": "e1"})) as p:
        assert integrations.send_resend_notification("+1555") == "e1"
        assert "support@blackpinelab.com" in p.call_args.kwargs["json"]["from"]


@patch("app.integrations.send_resend_notification", side_effect=RuntimeError)
@patch("app.integrations.get_twilio_client")
def test_hook_failure_does_not_break_call(mock_client, _r, client):
    mock_client.return_value.messages.create.return_value.sid = "SM1"
    r = signed_post(client, "/api/consent",
                    {"From": "+15551234567", "Digits": "1"})
    assert r.status_code == 200


def test_say_uses_polly_neural_voice_by_default(monkeypatch):
    from twilio.twiml.voice_response import VoiceResponse
    from app import main
    monkeypatch.delenv("TWILIO_VOICE", raising=False)
    r = VoiceResponse()
    main.say(r, main.GREETING)
    xml = str(r)
    assert 'voice="Polly.Joanna-Neural"' in xml
    assert 'language="en-US"' in xml
    assert main.GREETING_VERSION == "2026-10-22-artemis-final"


def test_say_voice_configurable(monkeypatch):
    from twilio.twiml.voice_response import VoiceResponse
    from app import main
    monkeypatch.setenv("TWILIO_VOICE", "Polly.Matthew-Neural")
    r = VoiceResponse()
    main.say(r, "hi")
    assert 'voice="Polly.Matthew-Neural"' in str(r)
