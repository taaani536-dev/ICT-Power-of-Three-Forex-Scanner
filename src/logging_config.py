"""Logging with secret redaction."""
from __future__ import annotations

import logging
import os
import re
import sys

SECRET_ENV_NAMES = ("TWELVE_DATA_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
_PATTERNS = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_\-]{10,}"), "bot***REDACTED***"),
    (re.compile(r"(?i)(apikey=)[^&\s'\"]+"), r"\1***REDACTED***"),
    (re.compile(r"(?i)(apikey\s+)[A-Za-z0-9]{8,}"), r"\1***REDACTED***"),
]


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    secrets = [os.environ.get(n, "") for n in SECRET_ENV_NAMES] + list(extra_secrets or [])
    for s in secrets:
        if s and len(s) >= 4:
            text = text.replace(s, "***REDACTED***")
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    converter = __import__("time").gmtime

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(asctime)sZ %(levelname)-7s %(name)s: %(message)s",
                                            "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)
