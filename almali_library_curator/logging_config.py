"""Structured logging.

Every log line is one JSON object on one line, so the container's output can be
read by any log shipper. Tokens, API keys and passwords never reach a log
record: redaction happens in :func:`almali_library_curator.config` and in the callers
that build extra fields.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

_RESERVED_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_HANDLER_FLAG = "_library_curator_handler"


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Install the JSON handler on the root logger."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    root.addHandler(handler)
