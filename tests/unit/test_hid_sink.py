"""Unit tests for ``HidSink`` in ``smart_panel.core.sinks`` (T028).

The real-panel sink opens through ``panel_driver.device`` (so every ``dev.write``
stays inside the transport module), sends CRTDIS + CRTLIG on every successful
open, retries open with backoff 1, 2, 4, 8, 10 s (cap), and reopens on the
**first** mid-stream write failure — the T129 hardware policy: the panel
intermittently stops accepting OUT reports with no USB disconnect, and reopening
the handle recovers it on the first attempt every time. ``panel_unresponsive``
with a power-cycle hint applies only when the *open* keeps failing, after more
than 60 s of continuous failure.
"""

from __future__ import annotations

import struct

import pytest

from panel_driver.protocol import build_frame_packets
from smart_panel.core.sinks import HidSink, PanelSink, SinkState, SinkStatus


class FakeDevice:
    """A fake HID device that records the byte strings written to it."""

    def __init__(self, *, fail_frames: bool = False) -> None:
        self.writes: list[bytes] = []
        self.fail_frames = fail_frames

    def write(self, data: bytes) -> None:
        if self.fail_frames and data[6:11] == b"DRA\x00\x00":
            raise OSError("device stopped accepting OUT reports")
        self.writes.append(bytes(data))


class DeviceFactory:
    """Return a fresh :class:`FakeDevice` per open; optionally fail."""

    def __init__(self, *, fail_open: bool = False, fail_frames_on: set[int] = frozenset()) -> None:
        self.fail_open = fail_open
        self.fail_frames_on = set(fail_frames_on)
        self.opens = 0
        self.devices: list[FakeDevice] = []

    def __call__(self) -> FakeDevice:
        index = self.opens
        self.opens += 1
        if self.fail_open:
            raise OSError("no device found")
        device = FakeDevice(fail_frames=index in self.fail_frames_on)
        self.devices.append(device)
        return device


def _frame() -> list[bytes]:
    return build_frame_packets(b"\xff" * 100)


def test_hid_sink_satisfies_the_panel_sink_protocol(fake_clock) -> None:
    assert isinstance(HidSink(open_device=DeviceFactory(), clock=fake_clock), PanelSink)


def test_open_sends_init_and_reports_connected(fake_clock) -> None:
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=fake_clock)
    assert sink.state().state == SinkState.DISCONNECTED
    sink.open()
    assert sink.state().state == SinkState.CONNECTED
    device = factory.devices[0]
    assert len(device.writes) == 2
    assert device.writes[0][6:11] == b"DIS\x00\x00"
    assert device.writes[1][6:11] == b"LIG\x00\x00"


def test_every_write_keeps_the_report_id_prefix_and_is_1025_bytes(fake_clock) -> None:
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=fake_clock)
    sink.open()
    sink.send(_frame())
    device = factory.devices[0]
    assert device.writes
    for data in device.writes:
        assert data[0] == 0x00
        assert len(data) == 1025
    assert sink.state().frames == 1


def test_repeated_open_failure_follows_the_backoff_sequence(fake_clock) -> None:
    factory = DeviceFactory(fail_open=True)
    sink = HidSink(open_device=factory, clock=fake_clock)
    sink.open()
    assert factory.opens == 1
    # Not enough time has passed for the 1 s retry.
    sink.open()
    assert factory.opens == 1
    fake_clock.advance(1)
    sink.open()
    assert factory.opens == 2
    sink.open()
    assert factory.opens == 2
    for delay, expected in ((2, 3), (4, 4), (8, 5), (10, 6), (10, 7)):
        fake_clock.advance(delay)
        sink.open()
        assert factory.opens == expected
    assert sink.state().state == SinkState.DISCONNECTED


def test_write_failure_reopens_on_the_first_failure_and_resents(fake_clock) -> None:
    factory = DeviceFactory(fail_frames_on={0})
    sink = HidSink(open_device=factory, clock=fake_clock)
    sink.open()
    assert factory.opens == 1
    device1 = factory.devices[0]
    assert len(device1.writes) == 2  # init only

    packets = _frame()
    sink.send(packets)

    # The first mid-stream write failed, so the handle was reopened immediately
    # (not after 3 failures) and the frame was resent on the fresh handle.
    assert factory.opens == 2
    device2 = factory.devices[1]
    assert device2.writes[0][6:11] == b"DIS\x00\x00"  # init sent on the reopen
    assert device2.writes[1][6:11] == b"LIG\x00\x00"
    assert len(device2.writes) == 2 + len(packets)
    assert sink.state().state == SinkState.CONNECTED
    assert sink.state().frames == 1


def test_unresponsive_after_prolonged_open_failure(fake_clock) -> None:
    factory = DeviceFactory(fail_open=True)
    sink = HidSink(open_device=factory, clock=fake_clock, unresponsive_after_s=60.0)
    for _ in range(10):
        sink.open()
        fake_clock.advance(10)
    status: SinkStatus = sink.state()
    assert status.state == SinkState.UNRESPONSIVE
    assert "power-cycle" in (status.last_error or "")


def test_a_recovered_open_clears_the_unresponsive_state(fake_clock) -> None:
    factory = DeviceFactory(fail_open=True)
    sink = HidSink(open_device=factory, clock=fake_clock, unresponsive_after_s=60.0)
    for _ in range(10):
        sink.open()
        fake_clock.advance(10)
    assert sink.state().state == SinkState.UNRESPONSIVE

    factory.fail_open = False
    sink.open()
    assert sink.state().state == SinkState.CONNECTED
    assert sink.state().last_error is None


def test_open_sends_the_configured_brightness(fake_clock) -> None:
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=fake_clock, brightness=70)
    sink.open()
    lig = factory.devices[0].writes[1]
    assert struct.unpack("<H", lig[11:13])[0] == 70


def test_set_brightness_validates_and_reapplies_on_reopen(fake_clock) -> None:
    factory = DeviceFactory(fail_frames_on={0})
    sink = HidSink(open_device=factory, clock=fake_clock)
    sink.open()
    device1 = factory.devices[0]
    before = len(device1.writes)
    sink.set_brightness(80)
    assert len(device1.writes) == before + 1
    assert device1.writes[-1][6:11] == b"LIG\x00\x00"
    with pytest.raises(ValueError):
        sink.set_brightness(101)

    sink.send(_frame())  # first write fails -> reopen reapplies brightness 80
    lig = factory.devices[1].writes[1]
    assert struct.unpack("<H", lig[11:13])[0] == 80


def test_close_reports_disconnected(fake_clock) -> None:
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=fake_clock)
    sink.open()
    sink.close()
    assert sink.state().state == SinkState.DISCONNECTED
