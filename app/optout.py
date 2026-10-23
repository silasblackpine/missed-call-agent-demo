"""SMS opt-out store. Numbers that text a STOP keyword are blocked until they
text START or UNSTOP. In-memory by default (tests/local); ``app.storage.configure_from_env``
swaps in Postgres when ``DATABASE_URL`` is set."""
import logging

log = logging.getLogger("missed_call.optout")

STOP_KEYWORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_KEYWORDS = {"START", "UNSTOP"}
HELP_KEYWORDS = {"HELP"}


class MemoryStore:
    def __init__(self):
        self._blocked: set[str] = set()

    def block(self, number: str, keyword: str = "STOP") -> None:
        self._blocked.add(number)

    def unblock(self, number: str, keyword: str = "START") -> None:
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
    """Fail closed: if the store can't be read, treat the number as blocked."""
    if not number:
        return False
    try:
        return _store.is_blocked(number)
    except Exception:
        log.exception("opt-out lookup failed; treating %s as blocked", number)
        return True


def handle_keyword(number: str, body: str) -> str | None:
    """Record a keyword from an inbound text. Returns 'stop', 'start', 'help' or None."""
    word = (body or "").strip().upper().rstrip(".!")
    if not number:
        return None
    if word in STOP_KEYWORDS:
        _store.block(number, word)
        log.info("opt-out recorded for %s (%s)", number, word)
        return "stop"
    if word in START_KEYWORDS:
        _store.unblock(number, word)
        log.info("opt-in restored for %s (%s)", number, word)
        return "start"
    if word in HELP_KEYWORDS:
        return "help"
    return None
