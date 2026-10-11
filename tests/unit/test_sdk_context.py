"""Unit tests for the SDK context and clock in ``smart_panel.sdk.context`` (T019).

T019 names no test file in ``tasks.md``; this covers the pieces the runner and
``sdk.testing`` (T031) will build on: ``Sample`` freshness values, the
``Clock`` protocol and its system implementation, the read-only bus view a
``FrameContext`` exposes, and the capability gate on ``ctx.alert``.
"""

from __future__ import annotations

import time

import pytest

from smart_panel.sdk.context import (
    Clock,
    FrameContext,
    Freshness,
    InstanceContext,
    ReadOnlyBus,
    Sample,
    SystemClock,
)
from smart_panel.sdk.plugin import Manifest


def _manifest(capabilities: set[str]) -> Manifest:
    return Manifest(
        name="demo",
        version="0.1.0",
        sdk_version="1",
        summary="demo",
        capabilities=capabilities,
    )


def test_sample_carries_value_timestamp_and_freshness() -> None:
    sample = Sample(value=3, ts=123.0, freshness=Freshness.FRESH)
    assert sample.value == 3
    assert sample.freshness == "fresh"


def test_system_clock_satisfies_the_clock_protocol() -> None:
    clock = SystemClock()
    assert isinstance(clock, Clock)
    assert clock.monotonic() <= clock.monotonic()
    assert abs(clock.time() - time.time()) < 5


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def get(self, topic: str) -> Sample:
        return Sample(value=1, ts=0.0, freshness=Freshness.FRESH)

    def topics(self, prefix: str = "") -> list[str]:
        return ["sys.cpu"]

    def publish(self, topic: str, value: object, ttl_s: float | None = None) -> None:
        self.published.append((topic, value))


def test_read_only_bus_exposes_reads_but_blocks_publish() -> None:
    bus = _FakeBus()
    view = ReadOnlyBus(bus)
    assert view.get("sys.cpu").value == 1
    assert view.topics() == ["sys.cpu"]
    with pytest.raises(AttributeError):
        view.publish("sys.cpu", 2)  # type: ignore[attr-defined]
    assert bus.published == []


def test_frame_context_exposes_the_render_inputs() -> None:
    frame = FrameContext(
        size=(1920, 462),
        now=1.0,
        wall=1_700_000_000.0,
        frame_index=7,
        fonts=object(),
        icons=object(),
        bus=ReadOnlyBus(_FakeBus()),
        min_text_px=24,
    )
    assert frame.size == (1920, 462)
    assert frame.now == 1.0
    assert frame.min_text_px == 24


def test_alert_requires_the_alert_source_capability() -> None:
    routed: list[dict[str, object]] = []
    ctx = InstanceContext(
        name="c1",
        manifest=_manifest({"region"}),
        alert_handler=lambda **kwargs: routed.append(kwargs),
    )
    with pytest.raises(PermissionError):
        ctx.alert("info", "hello")
    assert routed == []


def test_alert_is_routed_when_the_capability_is_declared() -> None:
    routed: list[dict[str, object]] = []

    def handler(**kwargs: object) -> dict[str, object]:
        routed.append(kwargs)
        return {"state": "queued"}

    ctx = InstanceContext(
        name="c1",
        manifest=_manifest({"alert_source"}),
        alert_handler=handler,
    )
    result = ctx.alert("warning", "disk full", title="Disk", id="d1", ttl=30)
    assert result == {"state": "queued"}
    assert routed[0]["severity"] == "warning"
    assert routed[0]["ttl"] == 30


def test_spawn_and_db_delegate_and_raise_when_unwired() -> None:
    bare = InstanceContext(name="c1", manifest=_manifest({"region"}))
    with pytest.raises(RuntimeError):
        bare.spawn(lambda: None)
    with pytest.raises(RuntimeError):
        bare.db()

    ctx = InstanceContext(
        name="c1",
        manifest=_manifest({"region"}),
        db_factory=lambda: "connection",
        spawn_handler=lambda fn, interval_s, timeout_s, name: "task",
    )
    assert ctx.db() == "connection"
    assert ctx.spawn(lambda: None) == "task"
