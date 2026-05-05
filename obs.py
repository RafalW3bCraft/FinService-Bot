"""Observability helper. One line per meaningful event, key=value format,
no emojis, no banners. Output goes to stdout via the root logger."""
import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)


def log_event(kind: str, **fields: Any) -> None:
    """Emit a single-line audit record.

    Example:
        log_event("cmd", user=123, command="/add_offer", chat="private")
        -> "cmd user=123 command=/add_offer chat=private"
    """
    parts = [kind]
    for key, value in fields.items():
        parts.append(f"{key}={shlex.quote(str(value))}")
    logger.info(" ".join(parts))
