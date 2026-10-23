# Missed-Call Agent Demo

Every missed call is a lost lead. This small FastAPI service turns a missed call into a booked appointment: when nobody picks up, the caller is asked whether they'd like a booking link by text, and only callers who press 1 or say yes get one. Each answer is logged as a consent record. Optionally it logs the lead in HubSpot and emails the owner via Resend.

Built by [Black Pine Lab](https://blackpinelab.com).

## How it works

```mermaid
sequenceDiagram
    participant Caller
    participant Twilio
    participant App as FastAPI (Vercel)
    participant Phone as Business phone
    participant HubSpot
    participant Resend
    Caller->>Twilio: Calls business number
    Twilio->>App: POST /api/voice (signed)
    App-->>Twilio: TwiML <Dial timeout=20>
    Twilio->>Phone: Rings
    Phone--xTwilio: No answer / busy
    Twilio->>App: POST /api/dial-status (DialCallStatus=no-answer)
    App-->>Twilio: TwiML <Gather dtmf speech numDigits=1> + consent greeting
    Caller->>Twilio: Presses 1 / says "yes" (or hangs up)
    Twilio->>App: POST /api/consent (Digits / SpeechResult)
    App->>App: Log consent record (caller, UTC time, answer, CallSid, greeting_version)
    alt Pressed 1 or said yes (and not opted out)
        App->>Twilio: Send SMS with Calendly link
        Twilio->>Caller: "Hi, this is Black Pine Lab, sorry we missed your call!..."
    else No answer / other input
        App-->>Twilio: Polite goodbye, no text
    end
    opt ENABLE_HUBSPOT
        App->>HubSpot: Create contact + deal (first stage)
    end
    opt ENABLE_RESEND
        App->>Resend: Email owner from support@
    end
```

- Every webhook is verified with Twilio's `X-Twilio-Signature`.
- Optional hooks can fail without breaking the call flow.
- If `FORWARD_TO_NUMBER` is blank, every call is treated as missed (handy for demos).

## SMS consent (verbal opt-in)

No text is sent automatically. After a missed call the caller hears:

> Hi, you've reached Black Pine Lab. Sorry we missed your call. Would you like us to text you a link to book a time? We will send one initial text after your consent. If you reply, additional conversational responses may follow. Opt-out, help, or re-subscription confirmations may also be sent. Message and data rates may apply. Reply HELP for help or STOP to opt out at any time. Terms are at blackpinelab.com/terms and our privacy policy is at blackpinelab.com/privacy. Press 1 or say yes to receive the text. To skip the text, simply hang up.

- Pressing 1, or speech containing the word "yes", sends one SMS (unless the number has opted out, see below) and plays:

  > Thanks. We will attempt to send your Black Pine Lab booking link if your number is eligible. If you previously opted out, you must text START or UNSTOP to this number before texts can resume. Message and data rates may apply. Goodbye.

- Any other input or no input: no text, polite goodbye.
- SMS: `Hi, this is Black Pine Lab, sorry we missed your call! You can book a time here: {CALENDLY_URL} or reply with how we can help. Reply STOP to opt out.`
- Every answer is written as a JSON consent record (`caller`, `timestamp_utc`, `answer`, `call_sid`, `consented`, `greeting_version`) to the `missed_call.consent` logger. `greeting_version` (currently `2026-10-22-artemis-final`) records which script the caller heard, so consent can be matched to the published opt-in page. Plug in a persistent store with `app.consent.set_store(fn)`.

## Inbound SMS keywords (`/api/sms`)

`POST /api/sms` is the number's incoming-message webhook (signature-verified like the voice routes). Keywords are matched case-insensitively on the whole message:

| Keyword | Effect | App reply |
|---|---|---|
| `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT` | Number added to the opt-out list | None (empty TwiML) |
| `START`, `UNSTOP` | Number removed from the opt-out list | None (empty TwiML) |
| `HELP` | Nothing recorded | `Black Pine Lab: missed-call follow-up texts. Email support@blackpinelab.com. Reply STOP to opt out.` |

On toll-free numbers the carrier/Twilio sends the STOP and START confirmations itself, so the app deliberately does not send its own.

**Opt-out block on consent:** `/api/consent` never texts a number on the opt-out list. Pressing 1 or saying yes on a call does **not** clear an opt-out; only texting START or UNSTOP does. The consent record is still logged.

> **Demo only:** the opt-out list is kept in memory (`app/optout.py`) and is lost on restart or across serverless instances. Before activation it needs durable storage (swap in a store with `app.optout.set_store(...)`), and the Twilio number's incoming-message webhook must be set to `/api/sms`.

## Project layout

```
api/index.py          Vercel entrypoint
app/main.py           Webhooks + TwiML
app/consent.py        SMS consent records (pluggable store)
app/integrations.py   Twilio SMS, HubSpot, Resend
tests/                Unit tests (all external APIs mocked)
vercel.json           Routes /api/* to the app
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
cp .env.example .env   # fill in values
uvicorn app.main:app --reload
```

Expose it with a tunnel (e.g. `ngrok http 8000`) and set `PUBLIC_BASE_URL` to the tunnel URL so signature checks match.

## Deploy to Vercel (free tier)

1. Import this repo in Vercel (framework preset: Other).
2. Add the env vars from `.env.example` under Project → Settings → Environment Variables.
3. Set `PUBLIC_BASE_URL` to your production URL, e.g. `https://missed-call-agent-demo.vercel.app`, and redeploy.
4. Check `https://<your-app>/api/health` returns `{"ok": true}`.

## Configure the Twilio number

In Twilio Console → Phone Numbers → your number → Voice configuration:

| Setting | Value |
|---|---|
| A call comes in | Webhook, `https://<your-app>/api/voice`, HTTP POST |
| A message comes in (Messaging configuration) | Webhook, `https://<your-app>/api/sms`, HTTP POST |

The `/api/dial-status` callback is set automatically via the `<Dial action>` in the TwiML.

### Texting US numbers: toll-free vs A2P 10DLC

- **Local (10DLC) numbers** need A2P 10DLC brand + campaign registration before carriers deliver your SMS. Approval can take days.
- **Toll-free numbers** need toll-free verification, but are usually the fastest path for a demo.
- **Trial accounts** can only text verified phone numbers. Fine for testing on your own phone.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | yes | Twilio API + signature validation |
| `TWILIO_PHONE_NUMBER` | yes | Number SMS is sent from (E.164) |
| `PUBLIC_BASE_URL` | yes | Public URL used for signature validation |
| `CALENDLY_URL` | yes | Booking link texted to callers |
| `BUSINESS_NAME` | no | Used in the SMS |
| `FORWARD_TO_NUMBER`, `DIAL_TIMEOUT_SECONDS` | no | Ring a real phone first |
| `ENABLE_HUBSPOT`, `HUBSPOT_TOKEN`, `HUBSPOT_PIPELINE_ID`, `HUBSPOT_PIPELINE_STAGE_ID` | no | Create contact + deal |
| `ENABLE_RESEND`, `RESEND_API_KEY`, `RESEND_FROM`, `NOTIFY_EMAIL` | no | Owner notification email |
| `SKIP_TWILIO_VALIDATION` | no | Local dev only |

## License

MIT
