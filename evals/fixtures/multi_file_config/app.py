from config import GREETING_PREFIX


def greet(name: str) -> str:
    return f"{GREETING_PREFIX}, {name}"
