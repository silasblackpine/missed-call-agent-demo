"""Missed-call text-back agent: Twilio voice webhooks -> SMS booking link."""
import logging

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from . import integrations
from .config import env, flag

log = logging.getLogger("missed_call")
app = FastAPI(title="Missed-Call Agent Demo")

MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}


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
        resp.say("Sorry we missed you. We'll text you a booking link right away.")
        resp.redirect("/api/dial-status?DialCallStatus=no-answer", method="POST")
    return twiml(resp)


@app.post("/api/dial-status")
async def dial_status(request: Request):
    form = await verified_form(request)
    status = form.get("DialCallStatus") or request.query_params.get("DialCallStatus", "")
    caller = form.get("From", "")
    resp = VoiceResponse()
    if status in MISSED_STATUSES and caller:
        handle_missed_call(caller)
        resp.say("Sorry we couldn't pick up. We just texted you a link to book a time.")
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
