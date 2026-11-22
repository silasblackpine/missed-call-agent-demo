"""Missed-call text-back agent: Twilio voice webhooks -> SMS booking link."""
import logging
import re
from urllib.parse import urlencode

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


# Lenient spoken-consent check for Pine's send_text quote.
# Negation or deferral always wins. Otherwise a clear affirmative
# marker is enough, regardless of filler words. Anything else is refused.
_CONSENT_NEGATION_TOKENS = {
    "no", "nope", "nah", "not", "never", "dont", "wont", "cant", "cannot",
    "wouldnt", "shouldnt", "stop", "later", "maybe", "wait", "unsubscribe",
    "guess", "cancel", "decline", "declined", "refuse", "refused",
    "negative", "nothing", "neither", "nobody",
}
_CONSENT_NEGATION_PHRASES = (
    ("not", "now"),
    ("no", "thanks"),
    ("no", "thank"),
    ("hold", "on"),
    ("i", "guess"),
)
_CONSENT_AFFIRM_TOKENS = {
    "yes", "yeah", "yea", "yah", "yep", "yup", "ya",
    "sure", "ok", "okay", "alright", "fine", "please",
    "perfect", "absolutely", "definitely", "mhm", "mmhmm", "uhhuh",
}
_CONSENT_AFFIRM_PHRASES = (
    ("go", "ahead"),
    ("do", "it"),
    ("send", "it"),
    ("sounds", "good"),
    ("that", "works"),
    ("of", "course"),
    ("why", "not"),
    ("uh", "huh"),
    ("please", "do"),
    ("text", "me"),
    ("works", "for", "me"),
    ("all", "right"),
)
_CONSENT_WHY_NOT = ("why", "not")


def _consent_tokens(answer: str) -> tuple[list[str], bool]:
    """Lowercase a quote, strip apostrophes, and split on punctuation.

    Apostrophes are removed so "don't" and "don’t" both become "dont".
    Returns (tokens, has_question_mark).
    """
    lowered = answer.lower().replace("’", "'").replace("`", "'")
    has_question = "?" in lowered
    stripped = lowered.replace("'", "")
    tokens = re.sub(r"[^a-z0-9]+", " ", stripped).split()
    return tokens, has_question


def _has_ngram(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    phrase_list = list(phrase)
    return any(tokens[i:i + n] == phrase_list for i in range(len(tokens) - n + 1))


def _strip_ngram(tokens: list[str], phrase: tuple[str, ...]) -> list[str]:
    n = len(phrase)
    phrase_list = list(phrase)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        if tokens[i:i + n] == phrase_list:
            i += n
        else:
            out.append(tokens[i])
            i += 1
    return out


def is_clear_sms_consent(answer: object) -> bool:
    """True for a spoken quote that clearly agrees to the text.

    Missing, non-string, or blank quotes are not consent. A negation or
    deferral (including "yeah no" or "sure, not now") refuses the whole
    quote. A question mark refuses unless the words are just "why not".
    Otherwise any affirmative marker is enough, even with filler words.
    Quotes with no affirmative marker are refused.
    """
    if not isinstance(answer, str) or not answer.strip():
        return False
    tokens, has_question = _consent_tokens(answer)
    if not tokens:
        return False
    # "why not" is an affirmative; don't treat its "not" as a negation.
    remainder = _strip_ngram(tokens, _CONSENT_WHY_NOT)
    if has_question and remainder:
        return False
    if any(token in _CONSENT_NEGATION_TOKENS for token in remainder):
        return False
    if any(_has_ngram(remainder, phrase) for phrase in _CONSENT_NEGATION_PHRASES):
        return False
    if any(token in _CONSENT_AFFIRM_TOKENS for token in tokens):
        return True
    return any(_has_ngram(tokens, phrase) for phrase in _CONSENT_AFFIRM_PHRASES)


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


SCREEN_PROMPT = "Missed-call line. Press 1 to take it."
# Dial timeout is only the ring window. Screening is what stops voicemail
# from counting as an answer; a slightly tighter default just shortens the race.
DEFAULT_DIAL_TIMEOUT_SECONDS = 10
# Carriers withhold caller ID as these words, or as the keypad spelling.
_WITHHELD_CALLER_WORDS = ("anonymous", "unknown", "restricted", "unavailable", "withheld", "private")
_WITHHELD_CALLER_DIGITS = frozenset({
    "266696687",    # ANONYMOUS
    "7378742833",   # RESTRICTED
    "86282452253",  # UNAVAILABLE
    "8656696",      # UNKNOWN
})


def caller_last_four(number: str) -> str:
    """Space-separated last four digits, or "" when the number cannot be spoken."""
    raw = (number or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if any(word in lowered for word in _WITHHELD_CALLER_WORDS):
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 4 or digits in _WITHHELD_CALLER_DIGITS:
        return ""
    return " ".join(digits[-4:])


def screen_prompt_for(caller: str) -> str:
    last4 = caller_last_four(caller)
    if not last4:
        return SCREEN_PROMPT
    return f"Missed-call line, caller ending {last4}. Press 1 to take it."


def screen_whisper_url(caller: str) -> str:
    """Screen URL for the forwarded leg.

    That request's From is the Dial callerId, so the original caller is
    passed on the query string (URL-encoded) for the whisper.
    """
    caller = (caller or "").strip()
    if not caller:
        return "/api/voice/screen"
    return "/api/voice/screen?" + urlencode({"caller": caller})


def dial_was_bridged(form: dict) -> bool:
    """True only when Twilio connected the caller to the callee.

    After a Number url whisper, DialCallStatus is often "completed" even if
    the child leg hung up during screening (carrier voicemail, a decline, or
    no keypress). DialBridged is the field that distinguishes those cases.
    """
    return form.get("DialBridged", "").strip().lower() == "true"


@app.post("/api/voice")
async def voice(request: Request):
    form = await verified_form(request)
    # Ring FORWARD_TO_NUMBER first (same Dial timeout and action) with no
    # Polly "Connecting you now" preface — the caller only hears ringback.
    # answerOnBridge keeps ringing during the callee-only screen whisper.
    # callerId is the Twilio business number so the owner's carrier presents
    # that line. An empty TWILIO_PHONE_NUMBER omits callerId.
    # If the shop number is unset, fall through to /api/dial-status as a miss.
    resp = VoiceResponse()
    forward = env("FORWARD_TO_NUMBER")
    if forward:
        dial_kwargs = {
            "timeout": int(env("DIAL_TIMEOUT_SECONDS", str(DEFAULT_DIAL_TIMEOUT_SECONDS))),
            "action": "/api/dial-status",
            "method": "POST",
            "answer_on_bridge": True,
        }
        business_number = env("TWILIO_PHONE_NUMBER")
        if business_number:
            dial_kwargs["caller_id"] = business_number
        dial = resp.dial(**dial_kwargs)
        dial.number(forward, url=screen_whisper_url(form.get("From", "")))
    else:
        resp.redirect("/api/dial-status?DialCallStatus=no-answer", method="POST")
    return twiml(resp)


@app.post("/api/voice/screen")
async def voice_screen(request: Request):
    """Callee-only whisper: press 1 to bridge, otherwise hang up this leg.

    The inbound caller keeps hearing ringback (answerOnBridge) and never
    hears this prompt. Empty TwiML after a 1 lets Twilio bridge the calls.
    `caller` is the original From from the parent /api/voice request.
    """
    form = await verified_form(request)
    digits = (form.get("Digits") or "").strip()
    resp = VoiceResponse()
    if digits == "1":
        return twiml(resp)
    if digits:
        resp.hangup()
        return twiml(resp)
    gather = resp.gather(num_digits=1, timeout=5, action="/api/voice/screen", method="POST")
    say(gather, screen_prompt_for(request.query_params.get("caller", "")))
    resp.hangup()
    return twiml(resp)


@app.post("/api/dial-status")
async def dial_status(request: Request):
    form = await verified_form(request)
    caller = form.get("From", "")
    # A miss is any result that was not actually bridged, including
    # DialCallStatus=completed after the screen leg hung up. Bridged calls
    # end here. Option 2 then hands off silently to Pine; otherwise press-1.
    missed = bool(caller) and not dial_was_bridged(form)
    if flag("ELEVENLABS_DIRECT_ANSWER") and missed:
        return await answer_with_pine(form)
    resp = VoiceResponse()
    if missed:
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
# Stay under Twilio's ~15s voice-webhook limit: 10s per register-call attempt,
# then a silent <Redirect> to retry. Three attempts ≈ 30s before fail-safe.
DEFAULT_REGISTER_TIMEOUT_SECONDS = "10"
REGISTER_CALL_MAX_ATTEMPTS = 3
PINE_CONNECT_PATH = "/api/pine-connect"


def register_call_timeout() -> float:
    return float(env("ELEVENLABS_TIMEOUT_SECONDS", DEFAULT_REGISTER_TIMEOUT_SECONDS))


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
        timeout=register_call_timeout(),
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


def _pine_connect_attempt(request: Request) -> int:
    raw = request.query_params.get("attempt", "1")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _retry_pine_connect(attempt: int, after: str = "") -> Response:
    query = f"attempt={attempt + 1}"
    if after:
        query += f"&after={after}"
    resp = VoiceResponse()
    resp.redirect(f"{PINE_CONNECT_PATH}?{query}", method="POST")
    return twiml(resp)


def try_connect_to_pine(form: dict, attempt: int = 1, *, after: str = "") -> Response:
    """Register the in-progress call with ElevenLabs. On timeout, silently
    redirect so the next webhook attempt gets a fresh Twilio time budget.
    After REGISTER_CALL_MAX_ATTEMPTS, speak the fail-safe line and hang up.
    """
    caller = form.get("From", "")
    called = form.get("To", "") or env("TWILIO_PHONE_NUMBER")
    call_sid = form.get("CallSid", "")
    if caller and called and env("ELEVENLABS_API_KEY"):
        try:
            body = register_elevenlabs_call(
                caller, called, dynamic=pine_dynamic_variables(form)
            )
            log.info("register-call ok call_sid=%s attempt=%s", call_sid, attempt)
            return Response(content=body, media_type="application/xml")
        except httpx.TimeoutException:
            log.exception("register-call timeout call_sid=%s attempt=%s", call_sid, attempt)
            if attempt < REGISTER_CALL_MAX_ATTEMPTS:
                return _retry_pine_connect(attempt, after)
        except Exception:
            log.exception("register-call failed call_sid=%s attempt=%s", call_sid, attempt)
    else:
        log.error("pine-connect missing caller, called number, or API key; failing safe")
    resp = VoiceResponse()
    if after == "declined":
        say(resp, GOODBYE_NO_TEXT)
    else:
        say(resp, REGISTER_CALL_FAIL_SAFE)
    resp.hangup()
    return twiml(resp)


@app.post("/api/handoff")
async def handoff(request: Request):
    """Connect the caller to the ElevenLabs agent. Fail safe: on any error, end the
    call exactly as it would have ended without the hand-off."""
    form = await verified_form(request)
    after = request.query_params.get("after", "")
    if flag("ELEVENLABS_HANDOFF_ENABLED"):
        return try_connect_to_pine(form, _pine_connect_attempt(request), after=after)
    resp = VoiceResponse()
    if after == "declined":
        say(resp, GOODBYE_NO_TEXT)
    resp.hangup()
    return twiml(resp)


@app.post("/api/pine-connect")
async def pine_connect(request: Request):
    """Retry register-call after a timeout. Signature-verified. Silent until exhausted."""
    form = await verified_form(request)
    after = request.query_params.get("after", "")
    attempt = _pine_connect_attempt(request)
    log.info("pine-connect call_sid=%s attempt=%s", form.get("CallSid", ""), attempt)
    return try_connect_to_pine(form, attempt, after=after)


async def answer_with_pine(form: dict, attempt: int = 1) -> Response:
    """Silent answer: no greeting <Say>. Register with ElevenLabs and return its TwiML.

    On register-call timeout, silently redirect to /api/pine-connect for another
    attempt. After the last attempt, speak one short fail-safe line and hang up.
    """
    log.info("direct-pine inbound call_sid=%s attempt=%s", form.get("CallSid", ""), attempt)
    return try_connect_to_pine(form, attempt)


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
    consent_answer is a short quote of a clear yes, a consent row is stored,
    and the number is not blocked. Missing or unclear consent is not treated
    as yes.
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

    if not caller or message_type not in ALLOWED_MESSAGE_TYPES:
        return {"ok": False, "reason": "invalid_request"}

    if optout.is_blocked(caller):
        log.info("send-text blocked by opt-out for %s type=%s", caller, message_type)
        return {"ok": False, "reason": "opted_out"}

    raw_consent = params.get("consent_answer")
    if not isinstance(raw_consent, str) or not is_clear_sms_consent(raw_consent):
        log.info("send-text blocked: consent required for %s type=%s", caller, message_type)
        return {"ok": False, "reason": "consent_required"}
    consent_answer = raw_consent.strip()

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
