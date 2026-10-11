"""Core SQLite state: pushed topics, small JSON values, per-instance databases (T021).

Three related pieces live here:

* :class:`Store` — the core database ``<home>/state.db`` in WAL mode with two
  tables (``data-model.md``): ``topics(topic PK, value_json, ts, ttl_s)`` for
  pushed values and counts, and ``meta(key PK, value)`` for small bookkeeping
  values such as the persisted counter and metadata.
* :class:`KV` — the runner's ``ctx.kv``, a namespaced view over ``meta`` storing
  JSON values of at most 64 KiB each.
* :func:`open_instance_db` — the private ``sqlite3`` connection a plugin gets
  from ``ctx.db()`` (``<home>/state/<instance>.db``, WAL mode). Keeping plugin
  tables out of the core database is what lets a built-in plugin use only the
  public SDK.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from smart_panel.core import paths
from smart_panel.sdk.context import SystemClock

MAX_KV_BYTES = 64 * 1024  # 64 KiB per value (data-model.md)

_TOPICS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS topics ("
    "topic TEXT PRIMARY KEY, value_json TEXT NOT NULL, ts REAL NOT NULL, ttl_s REAL)"
)
_META_SCHEMA = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class Store:
    """The core ``state.db`` (pushed topics and the shared ``kv`` namespace).

    Args:
        path: Database path; defaults to ``<home>/state.db``.
        clock: Wall-clock source (injectable so timestamps are testable).
    """

    def __init__(self, path: str | Path | None = None, clock: Any = None) -> None:
        self.path = Path(path) if path is not None else paths.state_db()
        self._clock = clock if clock is not None else SystemClock()
        self._lock = threading.RLock()
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_TOPICS_SCHEMA)
        conn.execute(_META_SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------------ topics

    def set_topic(self, topic: str, value: Any, ttl_s: float | None = None) -> None:
        """Persist ``value`` for ``topic`` with its update time and TTL."""
        payload = json.dumps(value, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO topics(topic, value_json, ts, ttl_s) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(topic) DO UPDATE SET value_json=excluded.value_json, "
                "ts=excluded.ts, ttl_s=excluded.ttl_s",
                (topic, payload, self._clock.time(), ttl_s),
            )
            self._conn.commit()

    def get_topic(self, topic: str) -> dict[str, Any] | None:
        """Return ``{"value", "ts", "ttl_s"}`` for ``topic`` or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json, ts, ttl_s FROM topics WHERE topic = ?", (topic,)
            ).fetchone()
        if row is None:
            return None
        return {"value": json.loads(row[0]), "ts": row[1], "ttl_s": row[2]}

    def delete_topic(self, topic: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM topics WHERE topic = ?", (topic,))
            self._conn.commit()

    def topics(self, prefix: str = "") -> list[str]:
        """Sorted topic names, optionally filtered by ``prefix``."""
        with self._lock:
            rows = self._conn.execute("SELECT topic FROM topics").fetchall()
        return sorted(row[0] for row in rows if row[0].startswith(prefix))

    # -------------------------------------------------------------------- meta

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)),
            )
            self._conn.commit()

    def meta_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return json.loads(row[0])

    def meta_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            self._conn.commit()

    # ---------------------------------------------------------------------- kv

    def kv(self, namespace: str) -> KV:
        """A namespaced key/value view over the ``meta`` table."""
        return KV(self, namespace)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class KVError(ValueError):
    """A namespaced value did not conform (for example over 64 KiB)."""


class KV:
    """Namespaced JSON key/value storage backed by ``meta`` (``ctx.kv``).

    Keys are stored as ``kv:<namespace>:<key>`` so two instances never collide
    and the core schema stays exactly the two documented tables.
    """

    def __init__(self, store: Store, namespace: str) -> None:
        self._store = store
        self._namespace = namespace

    def _full(self, key: str) -> str:
        return f"kv:{self._namespace}:{key}"

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``; raise ``KVError`` over 64 KiB."""
        payload = json.dumps(value, default=str)
        if len(payload.encode("utf-8")) > MAX_KV_BYTES:
            raise KVError(
                f"value for {key!r} is {len(payload.encode('utf-8'))} bytes; "
                f"the maximum is {MAX_KV_BYTES} (64 KiB)"
            )
        self._store.meta_set(self._full(key), payload)

    def get(self, key: str, default: Any = None) -> Any:
        raw = self._store.meta_get(self._full(key))
        if raw is None:
            return default
        return json.loads(raw)

    def delete(self, key: str) -> None:
        self._store.meta_delete(self._full(key))

    def keys(self) -> list[str]:
        """Sorted keys in this namespace."""
        prefix = f"kv:{self._namespace}:"
        return sorted(
            key[len(prefix) :]
            for key in self._all_meta_keys()
            if key.startswith(prefix)
        )

    def _all_meta_keys(self) -> list[str]:
        with self._store._lock:
            rows = self._store._conn.execute("SELECT key FROM meta").fetchall()
        return [row[0] for row in rows]


def open_instance_db(
    name: str, *, state_dir: str | Path | None = None, path: str | Path | None = None
) -> sqlite3.Connection:
    """Open (creating if needed) a plugin's private database in WAL mode.

    Args:
        name: Instance name; the file is ``<state_dir>/<name>.db``.
        state_dir: Directory holding instance databases; defaults to
            ``<home>/state/``.
        path: Explicit database path (overrides ``state_dir``/``name``).

    Returns:
        A fresh stdlib ``sqlite3`` connection the caller owns.
    """
    if path is not None:
        target = Path(path)
    else:
        base = Path(state_dir) if state_dir is not None else paths.state_dir()
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"{name}.db"
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
