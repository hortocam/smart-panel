"""JSON-lines rotating log with a redaction filter and an in-memory ring (T022).

``status``/``logs`` read the last 200 events from the ring without touching
disk; the file rotates at 1 MB x 5. Any value registered as a secret is masked
before it is written, so a token can never leak through a log line (FR-049).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from smart_panel.core import paths

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_BACKUP_COUNT = 5
DEFAULT_RING_SIZE = 200
REDACTED = "***"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class RedactionFilter(logging.Filter):
    """Masks every registered secret value in logged text.

    Also usable directly via :meth:`redact` so structured payloads are cleaned
    before they are serialised.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []
        self._lock = threading.Lock()

    def register(self, value: Any) -> None:
        """Register a secret value; empty/None values are ignored."""
        if value is None:
            return
        text = str(value)
        if not text:
            return
        with self._lock:
            if text not in self._secrets:
                self._secrets.append(text)

    def redact(self, text: str) -> str:
        """Return ``text`` with every registered secret replaced by ``***``."""
        result = text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            result = result.replace(secret, REDACTED)
        return result

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class RunnerLog:
    """The runner's structured logger.

    Args:
        path: Log file path; defaults to ``<home>/runner.log``.
        max_bytes: Rotation size (default 1 MB).
        backup_count: Number of rotated files kept (default 5).
        ring_size: In-memory events kept for ``status``/``logs`` (default 200).
        level: Minimum level name.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        ring_size: int = DEFAULT_RING_SIZE,
        level: str = "info",
    ) -> None:
        self.path = Path(path) if path is not None else paths.log_file()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._redactor = RedactionFilter()
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._logger = logging.getLogger(f"smart_panel.runner.{id(self)}")
        self._logger.setLevel(_LEVELS.get(level, logging.INFO))
        self._logger.propagate = False
        self._logger.handlers.clear()
        handler = RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(self._redactor)
        self._logger.addHandler(handler)
        self._handler = handler

    def register_secret(self, value: Any) -> None:
        """Register a secret so it is masked in every future event."""
        self._redactor.register(value)

    @property
    def redactor(self) -> RedactionFilter:
        """The redaction filter, so other components share one secret set."""
        return self._redactor

    def debug(self, event: str, **fields: Any) -> None:
        self._emit("debug", event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit("info", event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit("warning", event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit("error", event, fields)

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        """The recorded events, oldest first; ``limit`` keeps the newest ones."""
        events = list(self._ring)
        if limit is not None:
            return events[-limit:]
        return events

    def close(self) -> None:
        """Flush and close the file handler."""
        self._handler.close()
        self._logger.removeHandler(self._handler)

    def _emit(self, level: str, event: str, fields: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"ts": time.time(), "level": level, "event": event}
        payload.update(fields)
        payload = self._redact_object(payload)
        line = json.dumps(payload, default=str)
        self._ring.append(json.loads(line))
        self._logger.log(_LEVELS[level], line)

    def _redact_object(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self._redactor.redact(obj)
        if isinstance(obj, dict):
            return {key: self._redact_object(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._redact_object(value) for value in obj]
        return obj
