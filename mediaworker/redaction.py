"""Redact transport credentials at logging and public-error boundaries."""
from __future__ import annotations

import logging
import re


_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9_+/.=:-]+", re.IGNORECASE)
_SECRET = re.compile(
    r"(?P<key>\b(?:[a-z][a-z0-9]*[_-])*(?:password|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|node[_-]?token|github[_-]?token|lease[_-]?token|aws[_-]?access[_-]?key[_-]?id|"
    r"(?:aws[_-]?)?secret[_-]?access[_-]?key|"
    r"x-amz-(?:signature|credential|security-token))\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)", re.IGNORECASE,
)


def redact(value: str) -> str:
    # Entire URLs are private: object keys, userinfo and fragments can contain
    # user data even without a signed query string.
    value = _URL.sub("[redacted-url]", value)
    value = _AUTH.sub(r"\1 [redacted]", value)
    return _SECRET.sub(lambda match: match["key"] + "[redacted]", value)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Format first so arguments, exceptions and chained tracebacks receive
        # the same protection. Do not mutate the record used by other handlers.
        return redact(super().format(record))
