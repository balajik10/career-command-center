"""Minimal structured logs deliberately exclude payloads and private configuration."""

import json
import logging
from typing import Any

from career_radar.security import redact


class RedactedJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"level": record.levelname, "event": redact(record.getMessage())})


def summary_log(event: str, **counts: int | float | bool) -> dict[str, Any]:
    return {"event": redact(event), "metrics": counts}
