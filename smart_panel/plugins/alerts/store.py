"""The alerts plugin's private database: history and missed rows (T068, FR-024).

The plugin owns its schema in ``<home>/state/<instance>.db`` (``ctx.db()``,
WAL mode) — keeping plugin tables out of the core database is what lets a
built-in plugin use only the public SDK (``data-model.md`` "Storage mapping").

Tables (exactly as named in ``data-model.md``)::

    alerts(id PK, ext_id, severity, title, message, source, duration_s,
           received_at, shown_at, ended_at, state, outcome)
    missed_alerts(id PK, ext_id, severity, title, message, received_at, outcome)
    meta(key PK, value)          -- the schema version

History rows are pruned on retention at least every 60 s; missed rows are *not*
pruned — they survive until ``alert clear-missed`` or a matching ``resolve``
(``data-model.md`` "History retention").
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from typing import Any

from smart_panel.plugins.alerts.model import SEVERITY_RANK, Alert

__all__ = ["AlertStore", "RETENTIONS", "retention_cutoff"]

SCHEMA_VERSION = 1
RETENTIONS = ("1h", "2h", "4h", "today")
_RETENTION_SECONDS = {"1h": 3600.0, "2h": 7200.0, "4h": 14400.0}

_ALERTS = (
    "CREATE TABLE IF NOT EXISTS alerts ("
    "id TEXT PRIMARY KEY, ext_id TEXT, severity TEXT NOT NULL, title TEXT,"
    " message TEXT NOT NULL, source TEXT, duration_s INTEGER NOT NULL,"
    " received_at REAL NOT NULL, shown_at REAL, ended_at REAL, state TEXT NOT NULL,"
    " outcome TEXT)"
)
_ALERTS_COLUMNS = (
    "id, ext_id, severity, title, message, source, duration_s, received_at,"
    " shown_at, ended_at, state, outcome"
)
_MISSED = (
    "CREATE TABLE IF NOT EXISTS missed_alerts ("
    "id TEXT PRIMARY KEY, ext_id TEXT, severity TEXT NOT NULL, title TEXT,"
    " message TEXT NOT NULL, received_at REAL NOT NULL, outcome TEXT)"
)
_MISSED_COLUMNS = "id, ext_id, severity, title, message, received_at, outcome"
_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def retention_cutoff(now_wall: float, retention: str) -> float:
    """The ``received_at`` before which history is pruned (``data-model.md``)."""
    key = str(retention)
    if key in _RETENTION_SECONDS:
        return float(now_wall) - _RETENTION_SECONDS[key]
    if key == "today":
        midnight = datetime.fromtimestamp(float(now_wall)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return midnight.timestamp()
    return float(now_wall) - _RETENTION_SECONDS["1h"]


class AlertStore:
    """History and missed rows in the plugin's own SQLite database.

    Args:
        conn: A stdlib ``sqlite3`` connection from ``ctx.db()`` (the plugin owns
            its schema and migrations).
        clock: Wall-clock source used when a caller omits ``now_wall``.
    """

    def __init__(self, conn: sqlite3.Connection, clock: Any = None) -> None:
        self._conn = conn
        self._clock = clock

    # ------------------------------------------------------------------ schema

    def init(self) -> None:
        """Create the tables (idempotent) and record the schema version."""
        for statement in (_ALERTS, _MISSED, _META):
            self._conn.execute(statement)
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def schema_version(self) -> int:
        """The schema version stored beside the tables (0 when never initialised)."""
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            return 0
        try:
            return int(json.loads(row[0]))
        except (TypeError, ValueError):  # pragma: no cover - a hand-edited database
            return 0

    def _now(self, now_wall: float | None) -> float:
        if now_wall is not None:
            return float(now_wall)
        if self._clock is not None:
            return float(self._clock.time())
        return time.time()

    # ----------------------------------------------------------------- history

    def upsert(self, alert: Alert) -> None:
        """Insert or update the history row for ``alert`` (its id is the key)."""
        self._conn.execute(
            f"INSERT OR REPLACE INTO alerts({_ALERTS_COLUMNS}) "  # noqa: S608 - fixed columns
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            alert.row(),
        )
        self._conn.commit()

    def get(self, alert_id: str) -> Alert | None:
        """The history row for ``alert_id``, or ``None``."""
        row = self._conn.execute(
            f"SELECT {_ALERTS_COLUMNS} FROM alerts WHERE id = ?", (alert_id,)  # noqa: S608
        ).fetchone()
        return Alert.from_row(row) if row is not None else None

    def delete(self, alert_id: str) -> None:
        self._conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        self._conn.commit()

    def list(
        self,
        *,
        state: str | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        """History rows, newest first, optionally filtered by state/age."""
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(float(since))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT {_ALERTS_COLUMNS} FROM alerts{where} "  # noqa: S608 - fixed columns
            "ORDER BY received_at DESC, rowid DESC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [Alert.from_row(row) for row in self._conn.execute(sql, params).fetchall()]

    def prune(self, now_wall: float | None = None, retention: str = "1h") -> int:
        """Delete history older than the retention window; return the count.

        Missed rows live in their own table and are never touched here.
        """
        cutoff = retention_cutoff(self._now(now_wall), retention)
        cursor = self._conn.execute("DELETE FROM alerts WHERE received_at < ?", (cutoff,))
        self._conn.commit()
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ missed

    def mark_missed(self, alert: Alert, outcome: str | None = None) -> None:
        """Record ``alert`` as missed (independently of history retention)."""
        self._conn.execute(
            f"INSERT OR REPLACE INTO missed_alerts({_MISSED_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                alert.id,
                alert.ext_id,
                alert.severity,
                alert.title,
                alert.message,
                float(alert.received_at),
                outcome or alert.outcome or "timed_out",
            ),
        )
        self._conn.commit()

    def unmark_missed(self, alert_id: str) -> None:
        """Drop the missed row for ``alert_id`` (a matching resolve)."""
        self._conn.execute("DELETE FROM missed_alerts WHERE id = ?", (alert_id,))
        self._conn.commit()

    def clear_missed(self, alert_id: str | None = None) -> int:
        """Clear one missed row, or every one; return how many were cleared."""
        if alert_id is None:
            cursor = self._conn.execute("DELETE FROM missed_alerts")
        else:
            cursor = self._conn.execute("DELETE FROM missed_alerts WHERE id = ?", (alert_id,))
        self._conn.commit()
        return int(cursor.rowcount or 0)

    def missed(self) -> list[Alert]:
        """Missed rows, newest first."""
        rows = self._conn.execute(
            f"SELECT {_MISSED_COLUMNS} FROM missed_alerts "  # noqa: S608
            "ORDER BY received_at DESC, rowid DESC"
        ).fetchall()
        return [
            Alert(
                id=row[0],
                ext_id=row[1],
                severity=row[2],
                title=row[3],
                message=row[4],
                received_at=float(row[5] or 0.0),
                outcome=row[6],
            )
            for row in rows
        ]

    def missed_summary(self) -> dict[str, Any]:
        """``{count, max_severity}`` — the payload of the ``alerts.missed`` topic."""
        rows = self._conn.execute("SELECT severity FROM missed_alerts").fetchall()
        severities = [str(row[0]) for row in rows]
        if not severities:
            return {"count": 0, "max_severity": None}
        highest = max(severities, key=lambda name: SEVERITY_RANK.get(name, 0))
        return {"count": len(severities), "max_severity": highest}
