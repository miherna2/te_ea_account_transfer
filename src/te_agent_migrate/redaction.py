"""Recursive secret redaction for console/report artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"
SECRET_KEY_RE = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|csrf|csrftoken|key_file|private_key)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b[a-z0-9]{32}\b", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def redact_value(value: Any) -> Any:
    """Return a copy of *value* with likely secrets removed."""
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if SECRET_KEY_RE.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return TOKEN_RE.sub(REDACTED, BEARER_RE.sub(f"Bearer {REDACTED}", value))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_value(item) for item in value]
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Redact sensitive HTTP header values while preserving header names."""
    return {
        name: REDACTED if SECRET_KEY_RE.search(name) else redact_value(value)
        for name, value in headers.items()
    }
