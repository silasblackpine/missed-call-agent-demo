"""Missed-call text-back agent: Twilio voice webhooks -> SMS booking link."""
import logging
import re

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from . import consent, integrations
from .config import env, flag

log = logging.getLogger("missed_call")
app = FastAPI(title="Missed-Call Agent Demo")

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}

GREETING = (
    "Hi, you've reached Black Pine Lab. Sorry we missed your call. "
    "Would you like us to text you a link to book a time? "
    "This is one text message per missed call. Message and data rates may apply. "
    "Reply HELP for help or STOP to opt out at any time. "
    "Terms are at blackpinelab.com/terms and our privacy policy is at blackpinelab.com/privacy. "
    "Press 1 or say yes to receive the text. To skip the text, simply hang up."
)
CONFIRMATION = (
    "Thanks, you're set to receive one text from Black Pine Lab with a booking link. "
    "Message and data rates may apply. Goodbye."
)
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
    consent.record_consent(
        caller=caller,
        call_sid=form.get("CallSid", ""),
        answer=f"digits:{digits}" if digits else f"speech:{speech}",
        consented=agreed,
    )
    resp = VoiceResponse()
    if agreed:
        handle_missed_call(caller)
        resp.say(CONFIRMATION)
    else:
        resp.say(GOODBYE_NO_TEXT)
    resp.hangup()
    return twiml(resp)


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
