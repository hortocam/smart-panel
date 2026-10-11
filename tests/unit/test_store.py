"""Unit tests for ``smart_panel.core.store`` (T021).

The store is the core SQLite database (``<home>/state.db``, WAL mode) holding
pushed topics in ``topics`` and small JSON values in ``meta``. ``ctx.kv`` is a
namespaced view over ``meta`` limited to 64 KiB per value, and ``ctx.db()`` is a
private ``sqlite3`` connection per plugin instance (``<home>/state/<name>.db``),
so two instances never see each other's tables.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smart_panel.core.store import KV, MAX_KV_BYTES, Store, open_instance_db


def test_topics_persist_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    first = Store(db)
    first.set_topic("push.cpu", 42, ttl_s=300)
    first.close()

    second = Store(db)
    record = second.get_topic("push.cpu")
    assert record is not None
    assert record["value"] == 42
    assert record["ttl_s"] == 300
    assert record["ts"] is not None
    second.close()


def test_state_db_is_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    Store(db).close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_missing_topic_returns_none_and_delete_removes_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    assert store.get_topic("nope") is None
    store.set_topic("push.a", 1)
    assert store.topics() == ["push.a"]
    store.delete_topic("push.a")
    assert store.get_topic("push.a") is None
    store.close()


def test_topics_prefix_filter(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    store.set_topic("push.a", 1)
    store.set_topic("push.b", 2)
    store.set_topic("sys.cpu", 3)
    assert store.topics("push.") == ["push.a", "push.b"]
    store.close()


def test_kv_is_namespaced_and_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    store = Store(db)
    store.kv("alpha").set("count", 7)
    assert store.kv("alpha").get("count") == 7
    assert store.kv("beta").get("count") is None
    store.close()

    reopened = Store(db)
    assert reopened.kv("alpha").get("count") == 7
    assert reopened.kv("beta").get("count") is None
    reopened.close()


def test_kv_get_returns_default_and_keys_are_namespaced(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    kv = store.kv("alpha")
    assert kv.get("missing", "fallback") == "fallback"
    kv.set("a", 1)
    kv.set("b", 2)
    assert kv.keys() == ["a", "b"]
    kv.delete("a")
    assert kv.keys() == ["b"]
    store.close()


def test_kv_rejects_a_value_over_64_kib(tmp_path: Path) -> None:
    kv: KV = Store(tmp_path / "state.db").kv("alpha")
    with pytest.raises(ValueError) as excinfo:
        kv.set("big", "x" * (MAX_KV_BYTES + 1))
    assert "64" in str(excinfo.value)
    # A value whose JSON encoding is exactly at the limit is accepted.
    kv.set("edge", "x" * (MAX_KV_BYTES - 2))
    assert kv.get("edge") == "x" * (MAX_KV_BYTES - 2)


def test_per_instance_db_isolation(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    conn_a = open_instance_db("a", state_dir=state_dir)
    conn_a.execute("CREATE TABLE t(x INTEGER)")
    conn_a.execute("INSERT INTO t VALUES (1)")
    conn_a.commit()
    assert conn_a.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    conn_b = open_instance_db("b", state_dir=state_dir)
    tables_b = {row[0] for row in conn_b.execute("SELECT name FROM sqlite_master")}
    assert "t" not in tables_b
    assert (state_dir / "a.db").exists()
    assert (state_dir / "b.db").exists()
    conn_a.close()
    conn_b.close()
