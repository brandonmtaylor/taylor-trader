from __future__ import annotations

import logging
import re
from pathlib import Path

from taylortrader.secrets import loaded_secret_values

MASK = "**********"

# Shapes of Alpaca credentials, masked even if they were never loaded through secrets.py.
_KEY_PATTERNS = [
    re.compile(r"\b(?:AK|PK|CK)[A-Z0-9]{14,30}\b"),  # key IDs
    re.compile(r"(?i)(secret[_-]?key|api[_-]?secret|authorization|apca-api-secret-key)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"),
]


MIN_SECRET_LEN = 6


def redact(text: str) -> str:
    for value in loaded_secret_values():
        # A value this short isn't a real key, and masking it would garble ordinary text and numbers.
        if value and len(value) >= MIN_SECRET_LEN:
            text = text.replace(value, MASK)
    text = _KEY_PATTERNS[0].sub(MASK, text)
    text = _KEY_PATTERNS[1].sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    return text


class RedactingFilter(logging.Filter):
    """Masks secret values in every log record, including exception tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            formatter = logging.Formatter()
            message += "\n" + formatter.formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(message)
        record.args = ()
        return True


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "taylortrader.log"))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
