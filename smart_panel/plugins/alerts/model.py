"""The ``Alert`` entity and its validation (T067, FR-017 to FR-024).

Types, ranges and the state machine are pinned by ``data-model.md`` ("Alert"):
``severity`` is one of ``info``/``warning``/``critical`` (anything else rejected),
``title`` <= 80 chars, ``message`` 1..280 chars of plain text with control
characters stripped, ``source`` <= 40 chars, and ``duration_s`` defaults by
severity (10/20/60) capped at ``max_duration_s`` (300 by default).

A ``message`` longer than the limit is *truncated*, not rejected (the caller
reports the truncation as a warning in the command result, as
``contracts/builtin-plugins.md`` requires); an empty message is rejected — "a
notification must communicate something" (the sentence that deliberately
distinguishes ``alert push`` from ``text set --text ""``).

The module imports only ``smart_panel.sdk`` and the standard library: built-in
plugins may not import ``smart_panel.core`` (FR-043).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_DURATIONS",
    "MAX_MESSAGE_CHARS",
    "MAX_SOURCE_CHARS",
    "MAX_TITLE_CHARS",
    "OUTCOMES",
    "SEVERITIES",
    "SEVERITY_RANK",
    "STATES",
    "Alert",
    "AlertValidationError",
    "build_alert",
    "parse_duration",
    "strip_control",
]

SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"info": 1, "warning": 2, "critical": 3}
DEFAULT_DURATIONS = {"info": 10, "warning": 20, "critical": 60}
DEFAULT_MAX_DURATION_S = 300
MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 280
MAX_SOURCE_CHARS = 40

STATES = ("queued", "displayed", "dismissed", "resolved", "timed_out", "expired_in_queue")
OUTCOMES = ("dismissed", "resolved", "timed_out", "expired_in_queue")

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)?\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class AlertValidationError(ValueError):
    """An alert field did not conform; the CLI reports it as ``invalid_input``."""


def strip_control(text: str) -> str:
    """Return ``text`` as plain single-line text: no control characters.

    Tabs and newlines become single spaces (so a multi-line agent message keeps
    its word separation), and every other Unicode control/format character is
    dropped. The result never contains markup or an escape sequence.
    """
    flattened = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    kept = [
        character
        for character in flattened
        if unicodedata.category(character) not in ("Cc", "Cf")
    ]
    return " ".join("".join(kept).split())


def parse_duration(value: Any, *, default: int | None = None) -> int:
    """Parse ``42``, ``"42"``, ``"30s"``, ``"5m"`` or ``"1h"`` as whole seconds."""
    if value is None:
        if default is None:
            raise AlertValidationError("a duration is required")
        return int(default)
    if isinstance(value, bool):
        raise AlertValidationError(f"expected a duration, got {value!r}")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = _DURATION.match(value)
        if match is not None:
            return int(float(match.group(1)) * _DURATION_UNITS[match.group(2) or "s"])
    raise AlertValidationError(f"expected a duration like '30s', '5m' or '1h', got {value!r}")


@dataclass
class Alert:
    """One alert: the history row plus its in-memory queue bookkeeping.

    ``shown_monotonic`` and ``waiting_since`` are runtime-only (never persisted):
    the countdown and the queue wait are measured with the monotonic clock, so a
    wall-clock jump or a suspend/resume neither extends nor cuts short a
    displayed alert (``data-model.md``).
    """

    id: str
    severity: str
    message: str
    title: str | None = None
    source: str | None = None
    duration_s: int = DEFAULT_DURATIONS["info"]
    received_at: float = 0.0
    shown_at: float | None = None
    ended_at: float | None = None
    state: str = "queued"
    outcome: str | None = None
    ext_id: str | None = None
    shown_monotonic: float | None = field(default=None, compare=False, repr=False)
    waiting_since: float | None = field(default=None, compare=False, repr=False)
    seq: int = field(default=0, compare=False, repr=False)

    @property
    def is_open(self) -> bool:
        """Whether this alert still occupies the display or the queue."""
        return self.state in ("queued", "displayed")

    def summary(self) -> dict[str, Any]:
        """The JSON shape the ``list``/``show`` verbs return."""
        return {
            "id": self.id,
            "ext_id": self.ext_id,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "source": self.source,
            "duration_s": self.duration_s,
            "received_at": self.received_at,
            "shown_at": self.shown_at,
            "ended_at": self.ended_at,
            "state": self.state,
            "outcome": self.outcome,
        }

    def row(self) -> tuple[Any, ...]:
        """The ``alerts`` table row, in column order (see :mod:`.store`)."""
        return (
            self.id,
            self.ext_id,
            self.severity,
            self.title,
            self.message,
            self.source,
            int(self.duration_s),
            float(self.received_at),
            self.shown_at,
            self.ended_at,
            self.state,
            self.outcome,
        )

    @classmethod
    def from_row(cls, row: Any) -> Alert:
        """Rebuild an :class:`Alert` from an ``alerts`` table row."""
        return cls(
            id=row[0],
            ext_id=row[1],
            severity=row[2],
            title=row[3],
            message=row[4],
            source=row[5],
            duration_s=int(row[6] or 0),
            received_at=float(row[7] or 0.0),
            shown_at=row[8],
            ended_at=row[9],
            state=row[10],
            outcome=row[11],
        )


def build_alert(
    severity: Any,
    message: Any,
    *,
    title: Any = None,
    source: Any = None,
    id: Any = None,  # noqa: A002 - the field is named ``id`` by the contract
    ttl: Any = None,
    received_at: float | None = None,
    durations: dict[str, Any] | None = None,
    max_duration_s: Any = DEFAULT_MAX_DURATION_S,
) -> Alert:
    """Validate the fields of one alert and return it.

    Raises:
        AlertValidationError: ``severity`` is not one of the three levels, the
            (control-stripped) ``message`` is empty, or ``ttl`` is below 1 second.
    """
    if severity not in SEVERITIES:
        raise AlertValidationError(
            f"severity must be one of {', '.join(SEVERITIES)}, got {severity!r}"
        )

    if not isinstance(message, str):
        raise AlertValidationError(f"message must be a string, got {type(message).__name__}")
    text = strip_control(message)
    if not text:
        raise AlertValidationError("message must not be empty")
    text = text[:MAX_MESSAGE_CHARS]

    title_text = None
    if title is not None:
        if not isinstance(title, str):
            raise AlertValidationError(f"title must be a string, got {type(title).__name__}")
        title_text = strip_control(title)[:MAX_TITLE_CHARS] or None

    source_text = None
    if source is not None:
        if not isinstance(source, str):
            raise AlertValidationError(f"source must be a string, got {type(source).__name__}")
        source_text = strip_control(source)[:MAX_SOURCE_CHARS] or None

    if id is not None and (not isinstance(id, str) or not id):
        raise AlertValidationError(f"id must be a non-empty string, got {id!r}")

    table = dict(DEFAULT_DURATIONS)
    table.update({key: value for key, value in (durations or {}).items() if key in SEVERITIES})
    if ttl is None:
        duration = parse_duration(table.get(severity), default=DEFAULT_DURATIONS[severity])
    else:
        duration = parse_duration(ttl)
        if duration < 1:
            raise AlertValidationError(f"ttl must be at least 1 second, got {ttl!r}")
    cap = parse_duration(max_duration_s, default=DEFAULT_MAX_DURATION_S)
    duration = max(1, min(duration, cap))

    return Alert(
        id=id if isinstance(id, str) and id else uuid.uuid4().hex,
        ext_id=id if isinstance(id, str) and id else None,
        severity=severity,
        message=text,
        title=title_text,
        source=source_text,
        duration_s=duration,
        received_at=float(received_at) if received_at is not None else 0.0,
    )
