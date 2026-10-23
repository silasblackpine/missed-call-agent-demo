"""SMS opt-out store. Numbers that text a STOP keyword are blocked until they
text START or UNSTOP. In-memory by default; swap with ``set_store`` for a
persistent backend (database, KV, etc.)."""
import logging

log = logging.getLogger("missed_call.optout")

STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP"}
HELP_KEYWORDS = {"HELP"}


class MemoryStore:
    def __init__(self):
        self._blocked: set[str] = set()

    def block(self, number: str) -> None:
        self._blocked.add(number)

    def unblock(self, number: str) -> None:
        self._blocked.discard(number)

    def is_blocked(self, number: str) -> bool:
        return number in self._blocked


_store = MemoryStore()


def set_store(store) -> None:
    global _store
    _store = store


def reset() -> None:
    set_store(MemoryStore())


def is_blocked(number: str) -> bool:
    return bool(number) and _store.is_blocked(number)


def handle_keyword(number: str, body: str) -> str | None:
    """Record a keyword from an inbound text. Returns 'stop', 'start', 'help' or None."""
    word = (body or "").strip().upper().rstrip(".!")
    if not number:
        return None
    if word in STOP_KEYWORDS:
        _store.block(number)
        log.info("opt-out recorded for %s (%s)", number, word)
        return "stop"
    if word in START_KEYWORDS:
        _store.unblock(number)
        log.info("opt-in restored for %s (%s)", number, word)
        return "start"
    if word in HELP_KEYWORDS:
        return "help"
    return None
