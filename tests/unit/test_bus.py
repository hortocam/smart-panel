"""Unit tests for ``smart_panel.core.bus`` (T020).

The bus is the one piece of shared mutable state between plugin workers and
data sources, so it is thread-safe and takes an injectable clock (the fake
clock in tests) to make freshness transitions deterministic.
"""

from __future__ import annotations

from smart_panel.core.bus import Bus
from smart_panel.sdk.context import Freshness
from tests.conftest import FakeClock


def test_never_set_topic_is_no_data(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    sample = bus.get("push.mail")
    assert sample.freshness == Freshness.NO_DATA
    assert sample.value is None


def test_publish_then_get_reports_the_value_and_wall_timestamp(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    bus.publish("push.mail", 12)
    sample = bus.get("push.mail")
    assert sample.value == 12
    assert sample.ts == fake_clock.time()


def test_freshness_transitions_with_the_fake_clock(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    bus.publish("sys.cpu", 42, ttl_s=10)
    assert bus.get("sys.cpu").freshness == Freshness.FRESH
    fake_clock.advance(9)
    assert bus.get("sys.cpu").freshness == Freshness.FRESH
    fake_clock.advance(2)
    assert bus.get("sys.cpu").freshness == Freshness.STALE


def test_a_topic_without_ttl_never_goes_stale(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    bus.publish("alerts.missed", {"count": 0})
    fake_clock.advance(1_000_000)
    assert bus.get("alerts.missed").freshness == Freshness.FRESH


def test_republish_refreshes_freshness(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    bus.publish("sys.cpu", 1, ttl_s=5)
    fake_clock.advance(10)
    assert bus.get("sys.cpu").freshness == Freshness.STALE
    bus.publish("sys.cpu", 2, ttl_s=5)
    assert bus.get("sys.cpu").freshness == Freshness.FRESH


def test_subscribe_receives_only_matching_topics(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    seen: list[tuple[str, object]] = []
    bus.subscribe("push.a", lambda topic, sample: seen.append((topic, sample.value)))
    bus.publish("push.a", 5)
    bus.publish("push.b", 9)
    assert seen == [("push.a", 5)]


def test_subscription_can_be_cancelled(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    seen: list[int] = []
    subscription = bus.subscribe("t", lambda topic, sample: seen.append(sample.value))
    bus.publish("t", 1)
    subscription.cancel()
    bus.publish("t", 2)
    assert seen == [1]


def test_topics_lists_by_prefix_in_sorted_order(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    for topic in ("push.b", "push.a", "sys.cpu", "other"):
        bus.publish(topic, 1)
    assert bus.topics("push.") == ["push.a", "push.b"]
    assert bus.topics() == ["other", "push.a", "push.b", "sys.cpu"]


def test_a_failing_subscriber_does_not_break_publishing(fake_clock: FakeClock) -> None:
    bus = Bus(clock=fake_clock)
    seen: list[int] = []

    def boom(topic: str, sample: object) -> None:
        raise RuntimeError("subscriber failed")

    bus.subscribe("t", boom)
    bus.subscribe("t", lambda topic, sample: seen.append(sample.value))
    bus.publish("t", 7)
    assert seen == [7]
    assert bus.get("t").value == 7
