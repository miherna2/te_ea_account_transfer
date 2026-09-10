"""Recursive secret redaction for console and report artifacts."""

import re

try:
    from collections.abc import Mapping, Sequence
except ImportError:  # Python implementations exposing ABCs only from collections.
    from collections import Mapping, Sequence


REDACTED = "[REDACTED]"
SECRET_KEY_RE = re.compile(
    r"(token|password|passwd|secret|authorization|cookie|csrf|csrftoken|key_file|private_key)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b[a-z0-9]{32}\b", re.IGNORECASE)


def redact_value(value):
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if SECRET_KEY_RE.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return TOKEN_RE.sub(REDACTED, BEARER_RE.sub("Bearer %s" % REDACTED, value))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_value(item) for item in value]
    return value


def redact_headers(headers):
    return {
        name: REDACTED if SECRET_KEY_RE.search(name) else redact_value(value)
        for name, value in headers.items()
    }
