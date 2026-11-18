"""Missed-call text-back agent: Twilio voice webhooks -> SMS booking link."""
import logging
import re

import httpx

import hmac

from fastapi import FastAPI, Header, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from . import consent, integrations, optout, storage
from .config import env, flag

log = logging.getLogger("missed_call")
app = FastAPI(title="Missed-Call Agent Demo")
storage.configure_from_env()

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}

GREETING_VERSION = "2026-11-18-a1"
GREETING = (
    "Hi, you've reached Black Pine Lab. This call may be recorded. "
    "Sorry we missed you. Press 1 or say yes to get one booking-link text after your consent. "
    "Message and data rates may apply. Reply HELP for help or STOP to opt out anytime. "
    "Terms: blackpinelab.com/terms. Privacy: blackpinelab.com/privacy. Hang up to skip."
)
CONFIRMATION = (
    "Thanks. If your number is eligible, you'll get a Black Pine Lab booking-link text. "
    "Message and data rates may apply. Previously opted out? Text START to this number first. "
    "One moment."
)
HELP_REPLY = ("Black Pine Lab: missed-call follow-up texts. Email support@blackpinelab.com. "
              "Reply STOP to opt out.")
CONSENT_STORE_FAILED = ("Sorry, we couldn't send a text right now. "
                        "Thanks for calling Black Pine Lab. Goodbye.")
GOODBYE_NO_TEXT = "No problem, we won't send a text. Thanks for calling Black Pine Lab. Goodbye."

ELEVENLABS_REGISTER_CALL_URL = "https://api.elevenlabs.io/v1/convai/twilio/register-call"
DEFAULT_ELEVENLABS_AGENT_ID = "agent_6201m5xvvahmfm9ajahpbepqt8vx"


def is_yes(digits: str, speech: str) -> bool:
    if digits:
        return digits.strip() == "1"
    return bool(re.search(r"\byes\b", (speech or "").lower()))


async def verified_form(request: Request) -> dict:
    form = dict(await request.form())
    if flag("SKIP_TWILIO_VALIDATION"):
        return form
    url = str(request.url)
    base = env("PUBLIC_BASE_URL")
    if base:
        url = base.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
    sig = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(env("TWILIO_AUTH_TOKEN")).validate(url, form, sig):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return form


DEFAULT_VOICE = "Polly.Joanna-Neural"
VOICE_LANGUAGE = "en-US"


def say(target, text: str) -> None:
    """Speak text with the configured Twilio voice (Amazon Polly neural by default)."""
    target.say(text, voice=env("TWILIO_VOICE") or DEFAULT_VOICE, language=VOICE_LANGUAGE)


def twiml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="application/xml")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/voice")
async def voice(request: Request):
    form = await verified_form(request)
    # Ring FORWARD_TO_NUMBER first (same Dial timeout and action) with no
    # Polly "Connecting you now" preface — the caller only hears ringback.
    # If the shop number is unset, fall through to /api/dial-status as a miss.
    resp = VoiceResponse()
    forward = env("FORWARD_TO_NUMBER")
    if forward:
        dial = resp.dial(
            timeout=int(env("DIAL_TIMEOUT_SECONDS", "20")),
            action="/api/dial-status",
            method="POST",
        )
        dial.number(forward)
    else:
        resp.redirect("/api/dial-status?DialCallStatus=no-answer", method="POST")
    return twiml(resp)


@app.post("/api/dial-status")
async def dial_status(request: Request):
    form = await verified_form(request)
    status = form.get("DialCallStatus") or request.query_params.get("DialCallStatus", "")
    caller = form.get("From", "")
    # Option 2: on a miss, skip press-1 / Polly consent and hand off silently
    # to Pine via register-call. Answered calls still just hang up.
    if flag("ELEVENLABS_DIRECT_ANSWER") and status in MISSED_STATUSES and caller:
        return await answer_with_pine(form)
    resp = VoiceResponse()
    if status in MISSED_STATUSES and caller:
        gather = resp.gather(input="dtmf speech", num_digits=1, action="/api/consent",
                             method="POST", timeout=6, speech_timeout="auto")
        say(gather, GREETING)
        say(resp, GOODBYE_NO_TEXT)
    resp.hangup()
    return twiml(resp)


@app.post("/api/consent")
async def consent_route(request: Request):
    form = await verified_form(request)
    caller = form.get("From", "")
    digits = form.get("Digits", "")
    speech = form.get("SpeechResult", "")
    agreed = bool(caller) and is_yes(digits, speech)
    record = consent.record_consent(
        caller=caller,
        call_sid=form.get("CallSid", ""),
        answer=f"digits:{digits}" if digits else f"speech:{speech}",
        consented=agreed,
        greeting_version=GREETING_VERSION,
    )
    resp = VoiceResponse()
    if agreed and not record["stored"]:
        # No durable consent record -> no text.
        log.error("consent not stored for %s; skipping SMS", caller)
        say(resp, CONSENT_STORE_FAILED)
    elif agreed:
        # A spoken/keypad yes never clears a STOP; only START/UNSTOP by text does.
        if optout.is_blocked(caller):
            log.info("caller %s opted out; skipping SMS", caller)
        else:
            try:
                handle_missed_call(caller)
            except Exception:
                # Twilio errors (toll-free not verified, and any other send failure)
                # must not drop the stored consent or the hand-off.
                log.exception("text-back failed for %s", caller)
        say(resp, CONFIRMATION)
        if flag("ELEVENLABS_HANDOFF_ENABLED"):
            # Consent record + STOP check + text-back above run first; a send
            # failure is logged and does not block the hand-off.
            resp.redirect("/api/handoff?after=consented", method="POST")
            return twiml(resp)
    else:
        if flag("ELEVENLABS_HANDOFF_ENABLED") and flag("ELEVENLABS_HANDOFF_ON_DECLINE") and caller:
            resp.redirect("/api/handoff?after=declined", method="POST")
            return twiml(resp)
        say(resp, GOODBYE_NO_TEXT)
    resp.hangup()
    return twiml(resp)


PINE_CONSENT_VERSION = "2026-11-18-pine-option2"
ALLOWED_MESSAGE_TYPES = {"booking_link"}
REGISTER_CALL_FAIL_SAFE = (
    "Sorry, we're having trouble connecting this call. Please try again in a moment."
)


def register_elevenlabs_call(caller: str, called: str, dynamic: dict | None = None) -> str:
    """Ask ElevenLabs for TwiML that connects this in-progress call to the agent."""
    payload = {
        "agent_id": env("ELEVENLABS_AGENT_ID") or DEFAULT_ELEVENLABS_AGENT_ID,
        "from_number": caller,
        "to_number": called,
        "direction": "inbound",
    }
    if dynamic:
        payload["conversation_initiation_client_data"] = {"dynamic_variables": dynamic}
    r = httpx.post(
        ELEVENLABS_REGISTER_CALL_URL,
        headers={"xi-api-key": env("ELEVENLABS_API_KEY")},
        json=payload,
        timeout=float(env("ELEVENLABS_TIMEOUT_SECONDS", "5")),
    )
    r.raise_for_status()
    body = r.text
    if "<Response" not in body:
        raise ValueError("register-call did not return TwiML")
    return body


def pine_dynamic_variables(form: dict) -> dict:
    """Dynamic vars shared by /api/handoff and the option-2 silent answer path."""
    caller = form.get("From", "")
    called = form.get("To", "") or env("TWILIO_PHONE_NUMBER")
    return {
        "caller_number": caller,
        "called_number": called,
        "call_sid": form.get("CallSid", ""),
        "sms_opted_out": "true" if optout.is_blocked(caller) else "false",
    }


@app.post("/api/handoff")
async def handoff(request: Request):
    """Connect the caller to the ElevenLabs agent. Fail safe: on any error, end the
    call exactly as it would have ended without the hand-off."""
    form = await verified_form(request)
    after = request.query_params.get("after", "")
    caller = form.get("From", "")
    called = form.get("To", "") or env("TWILIO_PHONE_NUMBER")
    if flag("ELEVENLABS_HANDOFF_ENABLED") and caller and called and env("ELEVENLABS_API_KEY"):
        try:
            return Response(
                content=register_elevenlabs_call(
                    caller, called, dynamic=pine_dynamic_variables(form)
                ),
                media_type="application/xml",
            )
        except Exception:
            log.exception("ElevenLabs hand-off failed for %s", caller)
    resp = VoiceResponse()
    if after == "declined":
        say(resp, GOODBYE_NO_TEXT)
    # after=consented: CONFIRMATION was already spoken (no "Goodbye.").
    resp.hangup()
    return twiml(resp)


async def answer_with_pine(form: dict) -> Response:
    """Silent answer: no greeting <Say>. Register with ElevenLabs and return its TwiML.

    On register-call failure, speak one short fail-safe line and hang up so the
    caller is not left in silence. That is the only Twilio TTS in this path.
    """
    caller = form.get("From", "")
    called = form.get("To", "") or env("TWILIO_PHONE_NUMBER")
    call_sid = form.get("CallSid", "")
    log.info("direct-pine inbound call_sid=%s", call_sid)
    if caller and called and env("ELEVENLABS_API_KEY"):
        blocked = optout.is_blocked(caller)
        try:
            body = register_elevenlabs_call(
                caller,
                called,
                dynamic=pine_dynamic_variables(form),
            )
            log.info("register-call ok call_sid=%s opted_out=%s", call_sid, blocked)
            return Response(content=body, media_type="application/xml")
        except Exception:
            log.exception("register-call failed call_sid=%s", call_sid)
    else:
        log.error("direct-pine missing caller, called number, or API key; failing safe")
    resp = VoiceResponse()
    say(resp, REGISTER_CALL_FAIL_SAFE)
    resp.hangup()
    return twiml(resp)


def _tool_secret_ok(provided: str | None) -> bool:
    expected = env("ELEVENLABS_TOOL_SECRET")
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided, expected)


@app.post("/api/tools/send-text")
async def send_text_tool(request: Request, x_tool_secret: str | None = Header(default=None)):
    """Server tool for Pine. Not attached to the agent in this draft.

    Reuses sms_consent / sms_opt_outs and the existing STOP rule: a spoken yes
    never clears a STOP; only START/UNSTOP by text does. No SMS is sent unless
    a consent row is stored and the number is not blocked.
    """
    if not _tool_secret_ok(x_tool_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    params = body["parameters"] if isinstance(body.get("parameters"), dict) else body
    caller = str(params.get("caller_number") or params.get("from_number") or "").strip()
    message_type = str(params.get("message_type") or "").strip().lower()
    call_sid = str(params.get("call_sid") or body.get("conversation_id") or "").strip()
    consent_answer = str(params.get("consent_answer") or "yes").strip()

    if not caller or message_type not in ALLOWED_MESSAGE_TYPES:
        return {"ok": False, "reason": "invalid_request"}

    if optout.is_blocked(caller):
        log.info("send-text blocked by opt-out for %s type=%s", caller, message_type)
        return {"ok": False, "reason": "opted_out"}

    record = consent.record_consent(
        caller=caller,
        call_sid=call_sid,
        answer=f"pine_tool:{message_type}:{consent_answer}",
        consented=True,
        greeting_version=PINE_CONSENT_VERSION,
    )
    if not record["stored"]:
        log.error("consent not stored for %s; skipping SMS", caller)
        return {"ok": False, "reason": "consent_store_failed"}

    try:
        handle_missed_call(caller)
        return {"ok": True, "reason": "sent", "message_type": message_type}
    except Exception:
        log.exception("send-text failed for %s", caller)
        return {"ok": False, "reason": "send_failed"}


@app.post("/api/sms")
async def inbound_sms(request: Request):
    """Inbound SMS webhook: record opt-out/opt-in keywords and answer HELP.

    STOP/START confirmations are sent by the carrier on toll-free numbers,
    so this route deliberately returns an empty response for them.
    """
    form = await verified_form(request)
    sender = form.get("From", "")
    keyword = optout.handle_keyword(sender, form.get("Body", ""))
    resp = MessagingResponse()
    if keyword == "help":
        resp.message(HELP_REPLY)
    return Response(content=str(resp), media_type="application/xml")


def handle_missed_call(caller: str) -> dict:
    result = {"sms_sid": integrations.send_booking_sms(caller)}
    for name, fn in (("hubspot", integrations.create_hubspot_lead),
                     ("resend", integrations.send_resend_notification)):
        try:
            result[name] = fn(caller)
        except Exception:  # optional hooks never break the call flow
            log.exception("%s hook failed", name)
            result[name] = None
    return result
