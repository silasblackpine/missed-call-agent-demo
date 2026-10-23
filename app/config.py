import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def flag(name: str) -> bool:
    return env(name).lower() in {"1", "true", "yes", "on"}
