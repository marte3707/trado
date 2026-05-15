import logging
import os
import re
from typing import Any

import structlog

# Telegram bot tokens have the shape "<digits>:<base64-ish>". They are passed in
# the URL of every API call we make, so any logged exception or response that
# echoes the URL would leak it. We redact them centrally here.
_TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
_TOKEN_PLACEHOLDER = "bot<redacted>"


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return _TELEGRAM_TOKEN_RE.sub(_TOKEN_PLACEHOLDER, value)
    if isinstance(value, dict):
        return {k: _redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(v) for v in value)
    return value


def _scrub_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _redact_secrets(v) for k, v in event_dict.items()}


def configure_logging() -> None:
    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            _scrub_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
