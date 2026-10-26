"""Logging with secret redaction."""

from __future__ import annotations

import logging
import re

_MASK = "***REDACTED***"
_TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAC[0-9a-f]{32}\b"),  # Twilio account sid
    re.compile(r"\b[0-9a-f]{32}\b"),  # Twilio auth token
)
_LABELLED = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|auth[_-]?token|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"']{6,}"),
)


def redact(text: str) -> str:
    for p in _TOKEN_PATTERNS:
        text = p.sub(_MASK, text)
    for p in _LABELLED:
        text = p.sub(lambda m: m.group(1) + _MASK, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        return True


def configure_logging(level: str = "INFO", *, quiet_console: bool = False) -> None:
    root = logging.getLogger("sunday")
    if getattr(root, "_configured", False):
        root.setLevel(level.upper())
        return
    root.setLevel(level.upper())
    root.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    handler.addFilter(RedactingFilter())
    if quiet_console:
        handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    root._configured = True  # type: ignore[attr-defined]
    for noisy in ("httpx", "openai", "urllib3", "twilio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"sunday.{name}")
