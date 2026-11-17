import json
import os
import re


class PolicyDenied(ValueError):
    pass


def secret_values() -> list[str]:
    return [
        v
        for k, v in os.environ.items()
        if len(v) >= 8 and any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
    ]


def ensure_no_secrets(value: object) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if any(secret in text for secret in secret_values()) or re.search(
        r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|AIza[A-Za-z0-9_-]{30,})",
        text,
    ):
        # Never echo back the rejected value.
        raise PolicyDenied("secret-like content detected; content was not persisted or sent")


def safe_error(exc: Exception) -> str:
    # Remote error bodies, URLs and exception reprs may contain credentials or user input.
    return type(exc).__name__
