"""Consent records for SMS opt-in (proof of verbal/keypad consent).

Every answer to the consent prompt is logged as one JSON line on the
``missed_call.consent`` logger. Swap the store with ``set_store`` to persist
records elsewhere (database, CRM, etc.).
"""
import json
import logging
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("missed_call.consent")


def log_store(record: dict) -> None:
    log.info(json.dumps(record, sort_keys=True))


_store: Callable[[dict], None] = log_store


def set_store(fn: Callable[[dict], None]) -> None:
    global _store
    _store = fn


def record_consent(caller: str, call_sid: str, answer: str, consented: bool) -> dict:
    record = {
        "event": "sms_consent",
        "caller": caller,
        "call_sid": call_sid,
        "answer": answer,
        "consented": consented,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _store(record)
    except Exception:  # never break the call over logging
        logging.getLogger("missed_call").exception("consent store failed")
    return record
