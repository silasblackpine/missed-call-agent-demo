# send_text server tool (draft — not attached to Pine)

Draft only. This describes the webhook already coded in `app/main.py` as
`POST /api/tools/send-text`. It is **not** registered on the ElevenLabs agent
`agent_6201m5xvvahmfm9ajahpbepqt8vx`. Attaching it needs a separate Brent
approval (and a prompt patch so Pine asks for SMS consent).

## Endpoint

- **Method / path:** `POST /api/tools/send-text`
- **Live URL (after a future deploy):** `https://<PUBLIC_BASE_URL>/api/tools/send-text`
- **Auth:** header `X-Tool-Secret` must match env `ELEVENLABS_TOOL_SECRET` (constant-time compare). If the secret is unset or the header is missing/wrong, the route returns **401** and sends nothing.
- **Content type:** `application/json`

## Parameters

Accepted either flat or nested under `parameters` (ElevenLabs webhook style).

| Name | Required | Description |
|---|---|---|
| `caller_number` | yes | E.164 caller to text. Alias: `from_number`. |
| `message_type` | yes | Only `booking_link` is allowed in this draft. Anything else returns `invalid_request` and sends nothing. |
| `call_sid` | no | Twilio CallSid or ElevenLabs conversation id, stored on the consent row. Falls back to top-level `conversation_id`. |
| `consent_answer` | yes | Short quote of the caller's yes, in their words (for example `yes`, `yeah`, `yes please`, `yeah sure`, `sure, text me`). Required. The server does not default this to yes. |

## Server-side checks (must all pass)

Reuses the existing stores. No new consent/opt-out tables.

1. Auth as above.
2. Validate `caller_number` and `message_type`.
3. **`optout.is_blocked(caller)`** — reads `sms_opt_outs`. If blocked, return `{ok: false, reason: "opted_out"}` and **do not** write a new consent or send. A spoken yes on the call does **not** clear a STOP; only an inbound `START` / `UNSTOP` to `/api/sms` does.
4. **Clear yes quote.** `consent_answer` is required and must be a short quote of the caller's yes. If it is missing, null, not a string, empty or whitespace, contains a negation (`no`, `not now`, `don't text me`, including a negation mixed with yes), or is anything else that is not clearly affirmative, return `{ok: false, reason: "consent_required"}`. Do **not** write a consent row and do **not** send. Ambiguous answers are refused. The server is the control: a missing quote is not treated as yes.
5. **`consent.record_consent(...)`** — only after that quote passes. Writes `sms_consent` with `greeting_version=2026-11-18-pine-option2`, `consented=true`, `call_sid`, `timestamp_utc`, and `answer=pine_tool:{message_type}:{consent_answer}` (the quote as sent, trimmed). If the write fails (`stored=false`), return `consent_store_failed` and **do not** send.
6. **`handle_missed_call(caller)`** — same helper as `/api/consent`: `integrations.send_booking_sms` (Calendly link + STOP footer) plus optional HubSpot / Resend hooks.

`/api/sms` is unchanged: STOP/START/UNSTOP/HELP keyword handling and carrier-sent toll-free confirmations stay there.

## Responses

```json
{"ok": true, "reason": "sent", "message_type": "booking_link"}
{"ok": false, "reason": "invalid_request"}
{"ok": false, "reason": "opted_out"}
{"ok": false, "reason": "consent_required"}
{"ok": false, "reason": "consent_store_failed"}
{"ok": false, "reason": "send_failed"}
```

HTTP 401 is only for a bad/missing tool secret. Other failures are 200 + `ok: false` so the agent can tell the caller.

## ElevenLabs tool config (do not attach yet)

When Brent approves attaching this tool:

- Type: webhook (server tool)
- URL: `https://<PUBLIC_BASE_URL>/api/tools/send-text`
- Header: `X-Tool-Secret: <ELEVENLABS_TOOL_SECRET>` (from a secret store, never pasted into a chat or a committed file)
- Request body parameters as in the table above. Prefer filling `caller_number` from the `{{caller_number}}` dynamic variable passed at register-call time, not by asking Pine to invent it. Mark `consent_answer` required: it must be a short quote of the caller's yes. The server rejects the call when that quote is missing or not a clear yes.
