"""Missed-call text-back agent: Twilio voice webhooks -> SMS booking link."""
import logging
import re

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from . import consent, integrations, optout, storage
from .config import env, flag

log = logging.getLogger("missed_call")
app = FastAPI(title="Missed-Call Agent Demo")
storage.configure_from_env()

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}

GREETING_VERSION = "2026-10-22-artemis-final"
GREETING = (
    "Hi, you've reached Black Pine Lab. Sorry we missed your call. "
    "Would you like us to text you a link to book a time? "
    "We will send one initial text after your consent. "
    "If you reply, additional conversational responses may follow. "
    "Opt-out, help, or re-subscription confirmations may also be sent. "
    "Message and data rates may apply. "
    "Reply HELP for help or STOP to opt out at any time. "
    "Terms are at blackpinelab.com/terms and our privacy policy is at blackpinelab.com/privacy. "
    "Press 1 or say yes to receive the text. To skip the text, simply hang up."
)
CONFIRMATION = (
    "Thanks. We will attempt to send your Black Pine Lab booking link if your number is eligible. "
    "If you previously opted out, you must text START or UNSTOP to this number before texts can resume. "
    "Message and data rates may apply. Goodbye."
)
HELP_REPLY = ("Black Pine Lab: missed-call follow-up texts. Email support@blackpinelab.com. "
              "Reply STOP to opt out.")
CONSENT_STORE_FAILED = ("Sorry, we couldn't send a text right now. "
                        "Thanks for calling Black Pine Lab. Goodbye.")
GOODBYE_NO_TEXT = "No problem, we won't send a text. Thanks for calling Black Pine Lab. Goodbye."


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


def twiml(resp: VoiceResponse) -> Response:
    return Response(content=str(resp), media_type="application/xml")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/voice")
async def voice(request: Request):
    await verified_form(request)
    resp = VoiceResponse()
    forward = env("FORWARD_TO_NUMBER")
    if forward:
        resp.say("Thanks for calling. Connecting you now.")
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
    resp = VoiceResponse()
    if status in MISSED_STATUSES and caller:
        gather = resp.gather(input="dtmf speech", num_digits=1, action="/api/consent",
                             method="POST", timeout=6, speech_timeout="auto")
        gather.say(GREETING)
        resp.say(GOODBYE_NO_TEXT)
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
        resp.say(CONSENT_STORE_FAILED)
    elif agreed:
        # A spoken/keypad yes never clears a STOP; only START/UNSTOP by text does.
        if optout.is_blocked(caller):
            log.info("caller %s opted out; skipping SMS", caller)
        else:
            handle_missed_call(caller)
        resp.say(CONFIRMATION)
    else:
        resp.say(GOODBYE_NO_TEXT)
    resp.hangup()
    return twiml(resp)


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
