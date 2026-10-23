"""Outbound integrations: Twilio SMS, HubSpot CRM, Resend email."""
import logging

import httpx

from .config import env, flag

log = logging.getLogger("missed_call")

HUBSPOT_API = "https://api.hubapi.com"
RESEND_API = "https://api.resend.com/emails"


def get_twilio_client():
    from twilio.rest import Client

    return Client(env("TWILIO_ACCOUNT_SID"), env("TWILIO_AUTH_TOKEN"))


def send_booking_sms(to_number: str) -> str:
    body = (
        f"Sorry we missed your call to {env('BUSINESS_NAME', 'our team')}! "
        f"Book a time that works for you: {env('CALENDLY_URL')}"
    )
    msg = get_twilio_client().messages.create(
        to=to_number, from_=env("TWILIO_PHONE_NUMBER"), body=body
    )
    return msg.sid


def create_hubspot_lead(phone: str) -> dict | None:
    if not flag("ENABLE_HUBSPOT"):
        return None
    headers = {"Authorization": f"Bearer {env('HUBSPOT_TOKEN')}"}
    with httpx.Client(base_url=HUBSPOT_API, headers=headers, timeout=10) as c:
        contact = c.post(
            "/crm/v3/objects/contacts",
            json={"properties": {"phone": phone, "lifecyclestage": "lead"}},
        )
        contact.raise_for_status()
        contact_id = contact.json()["id"]
        deal = c.post(
            "/crm/v3/objects/deals",
            json={
                "properties": {
                    "dealname": f"Missed call {phone}",
                    "pipeline": env("HUBSPOT_PIPELINE_ID", "default"),
                    "dealstage": env("HUBSPOT_PIPELINE_STAGE_ID"),
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 3}
                        ],
                    }
                ],
            },
        )
        deal.raise_for_status()
        return {"contact_id": contact_id, "deal_id": deal.json()["id"]}


def send_resend_notification(phone: str) -> str | None:
    if not flag("ENABLE_RESEND"):
        return None
    resp = httpx.post(
        RESEND_API,
        headers={"Authorization": f"Bearer {env('RESEND_API_KEY')}"},
        json={
            "from": env("RESEND_FROM", "Black Pine Lab <support@blackpinelab.com>"),
            "to": [env("NOTIFY_EMAIL")],
            "subject": f"Missed call from {phone} - booking link sent",
            "text": f"A caller at {phone} was texted your booking link: {env('CALENDLY_URL')}",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("id")
