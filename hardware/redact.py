"""Helpers so secrets never reach logs, reports or the frontend."""
from __future__ import annotations

from typing import Any

SECRET_KEYS = {"key", "local_key", "localkey", "token", "secret", "password", "mesh_key", "netkey", "appkey"}


def mask(value: str | None, keep: int = 2) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


def redact(obj: Any) -> Any:
    """Return a deep copy of obj with secret-looking values masked."""
    if isinstance(obj, dict):
        return {
            k: (mask(v) if isinstance(v, str) and k.lower() in SECRET_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj
