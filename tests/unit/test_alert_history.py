"""Unit tests for alert history retention and the missed indicator (T064, FR-024).

Pinned by ``data-model.md`` ("History retention (clarified)", "Missed Alert and
indicator") and ``contracts/builtin-plugins.md`` (``alerts`` -> ``list``, ``show``,
``clear-missed``; ``alerts.missed``):

* retention windows ``1h`` (default), ``2h``, ``4h`` and ``today`` (since local
  midnight) measured from ``received_at``;
* pruning runs at least every 60 s;
* missed alerts survive pruning until cleared;
* a ``resolve`` removes a matching missed alert;
* ``dismissed`` and ``resolved`` are never counted as missed;
* ``alerts.missed`` publishes ``{count, max_severity}``;
* a wall-clock jump does not corrupt retention pruning.
"""

from __future__ import annotations

import sqlite3
import time

from smart_panel.core.bus import Bus
from smart_panel.plugins.alerts.model import build_alert
from smart_panel.plugins.alerts.queue import AlertManager
from smart_panel.plugins.alerts.store import AlertStore
from smart_panel.sdk.testing import FakeClock


def make_store(clock=None) -> AlertStore:
    clock = clock if clock is not None else FakeClock()
    store = AlertStore(sqlite3.connect(":memory:", check_same_thread=False), clock=clock)
    store.init()
    return store


def make_manager(clock=None, **options):
    clock = clock if clock is not None else FakeClock()
    store = make_store(clock)
    bus = Bus(clock=clock)
    manager = AlertManager(store, clock, bus=bus, **options)
    manager.start()
    return manager, store, bus, clock


def _alert(message: str, received_at: float, severity: str = "info"):
    return build_alert(severity, message, received_at=received_at)


# ------------------------------------------------------------- retention


def test_retention_windows_are_measured_from_received_at() -> None:
    now = time.time()
    for retention, hours in (("1h", 1), ("2h", 2), ("4h", 4)):
        store = make_store()
        store.upsert(_alert("fresh", now - (hours * 3600) + 60))
        store.upsert(_alert("old", now - (hours * 3600) - 60))
        removed = store.prune(now, retention)
        assert removed == 1, retention
        assert [alert.message for alert in store.list()] == ["fresh"], retention


def test_today_retention_prunes_before_local_midnight() -> None:
    store = make_store()
    now = time.time()
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    store.upsert(_alert("yesterday", midnight - 60))
    store.upsert(_alert("today", midnight + 60))
    removed = store.prune(now, "today")
    assert removed == 1
    assert [alert.message for alert in store.list()] == ["today"]


def test_pruning_removes_history_but_keeps_missed_rows() -> None:
    clock = FakeClock()
    manager, store, _bus, _clock = make_manager(clock, retention="1h")
    manager.submit(severity="info", message="dismissed", id="dismissed", ttl=1)
    manager.dismiss(id="dismissed")
    assert manager.missed_summary()["count"] == 0

    manager.submit(severity="critical", message="shown", id="shown", ttl=1)
    clock.advance(2)
    manager.advance()  # the duration elapsed: timed_out, and counted missed
    assert manager.missed_summary()["count"] == 1

    # Age every row past the retention window; the missed row must survive.
    clock.advance(7200)
    manager.housekeeping()
    assert [alert.message for alert in store.list()] == []
    assert [alert.message for alert in store.missed()] == ["shown"]


def test_wall_clock_jump_does_not_corrupt_retention_pruning() -> None:
    clock = FakeClock()
    store = make_store(clock)
    store.upsert(_alert("fresh", clock.time() - 60))

    # Pruning follows the wall clock: after a jump forward the row that was
    # inside the window is now outside it, and the call is idempotent.
    clock.advance(3700)
    assert store.prune(clock.time(), "1h") == 1
    assert store.list() == []
    assert store.prune(clock.time(), "1h") == 0

    # A backwards jump (an NTP correction) must not delete or resurrect rows.
    clock.advance(-7200)
    assert store.list() == []
    store.upsert(_alert("kept", clock.time() - 30))
    assert store.prune(clock.time(), "1h") == 0
    assert [alert.message for alert in store.list()] == ["kept"]


# ----------------------------------------------------------- missed alerts


def test_missed_summary_counts_and_highest_severity() -> None:
    manager, _store, bus, clock = make_manager()
    manager.submit(severity="warning", message="w", ttl=1)
    clock.advance(2)
    manager.advance()
    manager.submit(severity="info", message="i", ttl=1)
    clock.advance(2)
    manager.advance()
    assert manager.missed_summary() == {"count": 2, "max_severity": "warning"}
    sample = bus.get("alerts.missed")
    assert sample.value == {"count": 2, "max_severity": "warning"}
    assert sample.freshness == "fresh"  # no TTL: never goes stale


def test_dismissed_and_resolved_are_never_missed() -> None:
    manager, store, _bus, clock = make_manager()
    manager.submit(severity="critical", message="a", id="a", ttl=1)
    manager.submit(severity="critical", message="b", id="b", ttl=1)
    manager.dismiss(id="a")
    manager.resolve(id="b")
    clock.advance(5)
    manager.advance()
    assert manager.missed_summary()["count"] == 0
    assert {alert.id: alert.outcome for alert in store.list()} == {
        "a": "dismissed",
        "b": "resolved",
    }


def test_resolve_removes_a_matching_missed_alert() -> None:
    manager, _store, _bus, clock = make_manager()
    manager.submit(severity="critical", message="timeout", id="ups-1", ttl=1)
    clock.advance(2)
    manager.advance()
    assert manager.missed_summary()["count"] == 1
    result = manager.resolve(id="ups-1")
    assert result == {"id": "ups-1", "outcome": "resolved", "was": "timed_out"}
    assert manager.missed_summary()["count"] == 0


def test_clear_missed_clears_all_or_one() -> None:
    manager, _store, _bus, clock = make_manager()
    for index in range(3):
        manager.submit(severity="warning", message=f"m{index}", id=f"m{index}", ttl=1)
        clock.advance(2)
        manager.advance()
    assert manager.missed_summary()["count"] == 3
    assert manager.clear_missed(id="m0") == {"cleared": 1}
    assert manager.missed_summary() == {"count": 2, "max_severity": "warning"}
    assert manager.clear_missed() == {"cleared": 2}
    assert manager.missed_summary() == {"count": 0, "max_severity": None}


def test_show_reports_current_queue_and_missed() -> None:
    manager, _store, _bus, _clock = make_manager()
    shown = manager.submit(severity="warning", message="now", ttl=5)
    queued = manager.submit(severity="info", message="later")
    show = manager.show()
    assert show["current"]["id"] == shown["id"]
    assert show["current"]["state"] == "displayed"
    assert [alert["id"] for alert in show["queue"]] == [queued["id"]]
    assert show["missed"] == {"count": 0, "max_severity": None}
    assert show["queue"][0]["state"] == "queued"


def test_list_filters_by_state_and_since_and_is_newest_first() -> None:
    manager, _store, _bus, clock = make_manager()
    manager.submit(severity="info", message="first", id="first", ttl=3600)
    clock.advance(700)
    manager.submit(severity="critical", message="second", id="second", ttl=3600)
    listed = manager.list()
    assert [alert["id"] for alert in listed] == ["second", "first"]
    assert [alert["id"] for alert in manager.list(state="displayed")] == ["second"]
    assert [alert["id"] for alert in manager.list(since="10m")] == ["second"]
    assert [alert["id"] for alert in manager.list(limit=1)] == ["second"]


def test_startup_marks_a_displayed_alert_as_timed_out() -> None:
    clock = FakeClock()
    store = make_store(clock)
    alert = build_alert("critical", "was on screen", received_at=clock.time())
    alert.state = "displayed"
    alert.shown_at = clock.time()
    store.upsert(alert)
    manager = AlertManager(store, clock, bus=Bus(clock=clock))
    manager.start()
    assert manager.missed_summary()["count"] == 1
    assert store.get(alert.id).state == "timed_out"


def test_startup_expires_an_alert_that_waited_while_the_runner_was_down() -> None:
    clock = FakeClock()
    store = make_store(clock)
    store.upsert(build_alert("info", "waited", received_at=clock.time() - 600))
    manager = AlertManager(store, clock, bus=Bus(clock=clock))
    manager.start()
    assert manager.missed_summary()["count"] == 1
    assert store.list()[0].state == "expired_in_queue"


def test_an_alert_older_than_its_duration_is_expired_on_submit() -> None:
    """``queued`` -> ``expired_in_queue`` for a spooled alert that waited too long."""
    manager, store, _bus, clock = make_manager()
    stale = manager.submit(severity="info", message="while down", received_at=clock.time() - 600)
    assert stale["state"] == "expired_in_queue"
    assert store.get(stale["id"]).state == "expired_in_queue"
    assert manager.missed_summary()["count"] == 1
    assert manager.show()["current"] is None
