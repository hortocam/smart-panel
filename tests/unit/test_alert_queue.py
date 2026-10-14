"""Unit tests for the alerts queue rules (T063, FR-017 to FR-023).

Every rule quoted here is pinned by ``specs/001-status-display-platform/data-model.md``
(Alert / Queue rules / state machine) and ``contracts/builtin-plugins.md``
(``alerts`` settings):

* order by severity (critical, warning, info) then arrival;
* a strictly higher severity preempts the displayed alert, which returns to the
  front of its severity band, is not counted as missed, and gets its full display
  time again;
* queue cap 50 overflow drops the oldest ``info``, then ``warning``, recorded
  ``expired_in_queue``;
* ``max_wait_s`` default 120 moves a waiting alert to ``expired_in_queue``;
* default durations ``info`` 10, ``warning`` 20, ``critical`` 60 seconds, capped
  at ``max_duration_s`` default 300;
* the countdown starts at ``shown_at``, not ``received_at``;
* re-pushing an existing id updates it in place;
* ``show`` — the verb, and every ``status`` through ``describe_state`` — publishes
  ``alerts.missed`` when it is the first caller to notice a timeout;
* a wall-clock jump or suspend/resume neither extends nor cuts short a displayed
  alert, because timers use the monotonic clock.
"""

from __future__ import annotations

import sqlite3

import pytest

from smart_panel.core.bus import Bus
from smart_panel.plugins.alerts.model import (
    DEFAULT_DURATIONS,
    AlertValidationError,
    build_alert,
)
from smart_panel.plugins.alerts.queue import AlertManager
from smart_panel.plugins.alerts.store import AlertStore
from smart_panel.sdk.testing import FakeClock

# ------------------------------------------------------------------- helpers


class JumpClock:
    """A clock whose wall time can jump (or freeze) without the monotonic clock."""

    def __init__(self, monotonic: float = 0.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(monotonic)
        self._wall = float(wall)

    def monotonic(self) -> float:
        return self._monotonic

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward (a normal passage of time)."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)

    def jump_wall(self, seconds: float) -> None:
        """Move only the wall clock (a clock step, not elapsed time)."""
        self._wall += float(seconds)

    def freeze_wall(self, while_monotonic_moves: float) -> None:
        """Suspend/resume: monotonic keeps counting, wall does not."""
        self._monotonic += float(while_monotonic_moves)


def make_manager(clock=None, **options):
    """A started :class:`AlertManager` over an in-memory store."""
    clock = clock if clock is not None else FakeClock()
    store = AlertStore(sqlite3.connect(":memory:", check_same_thread=False), clock=clock)
    store.init()
    manager = AlertManager(store, clock, **options)
    manager.start()
    return manager, store, clock


# -------------------------------------------------------------------- model


def test_build_alert_rejects_unknown_severity_and_empty_message() -> None:
    with pytest.raises(AlertValidationError):
        build_alert("bogus", "hello")
    with pytest.raises(AlertValidationError):
        build_alert("info", "   ")
    with pytest.raises(AlertValidationError):
        build_alert("info", "\x00\x1b")  # control characters only: nothing communicates
    with pytest.raises(AlertValidationError):
        build_alert("info", "hello", ttl=0)


def test_build_alert_defaults_durations_by_severity_and_caps_them() -> None:
    for severity, expected in DEFAULT_DURATIONS.items():
        alert = build_alert(severity, "x")
        assert alert.duration_s == expected
    capped = build_alert("critical", "x", ttl=10_000)
    assert capped.duration_s == 300  # max_duration_s default
    capped = build_alert("info", "x", ttl=600, max_duration_s=120)
    assert capped.duration_s == 120
    explicit = build_alert("info", "x", ttl="45")  # noqa: the CLI may pass a string
    assert explicit.duration_s == 45


def test_build_alert_strips_control_characters_and_truncates_with_a_warning() -> None:
    alert = build_alert("info", "line\none\x07 two")
    assert alert.message == "line one two"
    long = "x" * 400
    alert = build_alert("info", long, title="t" * 200, source="s" * 100)
    assert len(alert.message) == 280
    assert len(alert.title) == 80
    assert len(alert.source) == 40


def test_build_alert_generates_an_id_and_keeps_a_caller_id() -> None:
    assert build_alert("info", "x").id
    assert build_alert("info", "x", id="ups-1").id == "ups-1"


# -------------------------------------------------------------- queue order


def test_queue_orders_by_severity_then_arrival() -> None:
    manager, _store, clock = make_manager()
    shown = manager.submit(severity="critical", message="first")
    assert shown["state"] == "displayed"
    info = manager.submit(severity="info", message="i1")
    warning = manager.submit(severity="warning", message="w1")
    info2 = manager.submit(severity="info", message="i2")
    assert [alert["id"] for alert in manager.show()["queue"]] == [
        warning["id"],
        info["id"],
        info2["id"],
    ]
    assert [alert["position"] for alert in manager.show()["queue"]] == [1, 2, 3]
    assert manager.show()["current"]["id"] == shown["id"]


def test_higher_severity_preempts_and_the_preempted_alert_keeps_its_place() -> None:
    manager, _store, clock = make_manager()
    first = manager.submit(severity="info", message="a", ttl=10)
    second = manager.submit(severity="info", message="b", ttl=10)
    clock.advance(5)
    preemptor = manager.submit(severity="critical", message="c", ttl=60)

    # The preempted alert is re-queued, not counted as missed.
    assert manager.show()["current"]["id"] == preemptor["id"]
    assert [alert["id"] for alert in manager.show()["queue"]] == [first["id"], second["id"]]
    assert manager.missed_summary() == {"count": 0, "max_severity": None}

    # Freeing the slot shows the preempted alert again with a *full* duration: it
    # had already run 5 of its 10 seconds before the preemption.
    manager.dismiss(id=preemptor["id"])
    assert manager.show()["current"]["id"] == first["id"]
    clock.advance(9)
    manager.advance()
    assert manager.show()["current"]["id"] == first["id"]  # not a leftover 5 s
    clock.advance(1.5)
    manager.advance()
    assert manager.show()["current"]["id"] == second["id"]


def test_queue_cap_drops_the_oldest_info_then_warning() -> None:
    manager, store, _clock = make_manager(queue_cap=5)
    manager.submit(severity="critical", message="shown", ttl=300)
    for index in range(3):
        manager.submit(severity="warning", message=f"w{index}")
    for index in range(3):
        manager.submit(severity="info", message=f"i{index}")

    queue = manager.show()["queue"]
    assert [alert["severity"] for alert in queue] == ["warning"] * 3 + ["info"] * 2
    dropped = [alert for alert in store.list() if alert.state == "expired_in_queue"]
    assert [alert.message for alert in dropped] == ["i0"]  # the oldest info went first
    assert manager.missed_summary()["count"] == 1


def test_queue_cap_falls_back_to_warning_when_no_info_waits() -> None:
    manager, store, _clock = make_manager(queue_cap=5)
    manager.submit(severity="critical", message="shown", ttl=300)
    for index in range(6):
        manager.submit(severity="warning", message=f"w{index}")
    dropped = [alert for alert in store.list() if alert.state == "expired_in_queue"]
    assert [alert.message for alert in dropped] == ["w0"]
    assert manager.missed_summary()["count"] == 1


def test_max_wait_expires_a_waiting_alert() -> None:
    manager, store, clock = make_manager()
    manager.submit(severity="critical", message="shown", ttl=300)
    waiting = manager.submit(severity="info", message="waiting", ttl=10)
    clock.advance(119)
    manager.advance()
    assert manager.show()["queue"][0]["id"] == waiting["id"]
    clock.advance(2)
    manager.advance()
    assert manager.show()["queue"] == []
    expired = store.get(waiting["id"])
    assert expired.state == "expired_in_queue"
    assert expired.outcome == "expired_in_queue"
    assert manager.missed_summary()["count"] == 1


def test_countdown_starts_at_shown_at_not_received_at() -> None:
    manager, _store, clock = make_manager()
    # ``a`` is received at t=0 but only reaches the panel at t=60, after ``b``.
    a = manager.submit(severity="info", message="a", ttl=10, received_at=clock.time())
    b = manager.submit(severity="critical", message="b", ttl=60)
    assert manager.show()["current"]["id"] == b["id"]
    clock.advance(60)
    manager.advance()  # b times out, a is shown for the first time
    assert manager.show()["current"]["id"] == a["id"]
    clock.advance(9.5)
    manager.advance()
    assert manager.show()["current"]["id"] == a["id"], "counted from received_at, not shown_at"
    clock.advance(0.6)
    manager.advance()
    assert manager.show()["current"] is None


def test_repush_of_the_same_id_updates_in_place() -> None:
    manager, store, _clock = make_manager()
    first = manager.submit(severity="warning", message="old", id="ups-1")
    second = manager.submit(severity="critical", message="new", id="ups-1")
    assert first["id"] == second["id"] == "ups-1"
    history = store.list()
    assert len(history) == 1, "a re-push must update the row, not add one"
    assert history[0].message == "new"
    assert history[0].severity == "critical"
    assert manager.show()["current"]["id"] == "ups-1"


def test_wall_clock_jump_does_not_touch_a_displayed_alert() -> None:
    clock = JumpClock()
    manager, _store, _clock = make_manager(clock)
    manager.submit(severity="warning", message="steady", ttl=20)
    clock.jump_wall(3600)  # an NTP step, or a laptop wake
    manager.advance()
    assert manager.show()["current"] is not None, "a wall jump cut the alert short"
    clock.freeze_wall(19)  # suspend: monotonic only
    manager.advance()
    assert manager.show()["current"] is not None
    clock.advance(1.5)
    manager.advance()
    assert manager.show()["current"] is None, "a frozen wall clock extended the alert"


# ---------------------------------------------------------------- missed topic


def test_show_publishes_the_missed_topic_when_it_is_the_first_to_notice() -> None:
    """``show`` (and so every ``status``) must publish ``alerts.missed`` too.

    Regression (t_bdb6e057): ``show()`` advanced the timers, expired the alert and
    marked it missed, but never called ``_publish_missed()``. When this call is the
    first to notice the timeout -- no render tick has run -- the topic kept its
    previous value, so the sidebar's built-in alert entry never saw the count.
    """
    clock = FakeClock()
    store = AlertStore(sqlite3.connect(":memory:", check_same_thread=False), clock=clock)
    store.init()
    bus = Bus(clock=clock)
    manager = AlertManager(store, clock, bus=bus)
    manager.start()
    manager.submit(severity="critical", message="on battery", id="ups-1", ttl=1)
    assert bus.get("alerts.missed").value == {"count": 0, "max_severity": None}

    clock.advance(2)  # the display time elapses; nothing else settles the timer
    show = manager.show()
    assert show["missed"] == {"count": 1, "max_severity": "critical"}
    assert bus.get("alerts.missed").value == {"count": 1, "max_severity": "critical"}
