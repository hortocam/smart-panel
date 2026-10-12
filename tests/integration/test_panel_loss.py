"""Integration test for panel loss (T042).

The runner must be safe to start with no panel attached (it reports "waiting for
panel"), begin sending frames the moment a device appears, and survive a device
that stops accepting writes: the sink reopens the handle, the init sequence is
resent on the new handle, and frames resume. The runner process never exits
(``research.md`` decisions 3 and 17; contract ``cli.md`` panel fields).

The real :class:`~smart_panel.core.sinks.HidSink` is driven with a fake
``open_device``/``write_frame`` device, so no panel is required and every byte
stays inside the transport's own write path.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from smart_panel.core.config import Config
from smart_panel.core.runner import Runner
from smart_panel.core.sinks import HidSink, SinkState
from tests.conftest import FakeClock

_DIS = b"DIS\x00\x00"
_LIG = b"LIG\x00\x00"
_DRA = b"DRA\x00\x00"


class FakeDevice:
    """A fake HID device recording the byte strings written to it."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.fail_frames = False
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.fail_frames and data[6:11] == _DRA:
            raise OSError("device stopped accepting OUT reports")
        self.writes.append(bytes(data))

    def close(self) -> None:
        self.closed = True

    def commands(self) -> list[bytes]:
        return [data[6:11] for data in self.writes]

    def frames(self) -> int:
        return sum(1 for command in self.commands() if command == _DRA)


class DeviceFactory:
    """An ``open_device`` callable: fails while ``available`` is False."""

    def __init__(self) -> None:
        self.available = False
        self.opens = 0
        self.devices: list[FakeDevice] = []

    def __call__(self) -> FakeDevice:
        self.opens += 1
        if not self.available:
            raise OSError("no device found")
        device = FakeDevice()
        self.devices.append(device)
        return device

    @property
    def current(self) -> FakeDevice:
        assert self.devices, "no device has been opened yet"
        return self.devices[-1]


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp base: AF_UNIX paths are capped near 108 bytes."""
    base = Path(tempfile.mkdtemp(prefix="ploss-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, short_tmp: Path) -> Path:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(short_tmp / "runtime"))
    monkeypatch.setenv("SMART_PANEL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    return short_tmp / "runtime"


def test_the_runner_survives_panel_appearance_and_loss(runtime: Path) -> None:
    sink_clock = FakeClock()
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=sink_clock, brightness=50)
    runner = Runner(Config(), sink=sink, frame_interval_s=0.02)
    runner.start()
    try:
        # 1. No device yet: the runner is up and reports waiting for the panel.
        status = runner.status()
        assert runner.state == "running"
        assert status["panel"]["state"] == str(SinkState.DISCONNECTED)
        assert status["panel"]["message"] == "waiting for panel"
        assert factory.opens >= 1  # it tried to open
        assert status["frames"]["sent"] == 0

        # 2. A device appears: frames start, and only after the init sequence.
        factory.available = True
        sink_clock.advance(60)  # clear any open backoff
        _wait_until(lambda: sink.state().frames > 0)
        first = factory.current
        assert first.commands()[:2] == [_DIS, _LIG]
        assert _DRA in first.commands()
        _wait_until(lambda: runner.status()["frames"]["sent"] > 0)

        # 3. The device stops accepting writes: the handle is reopened and the
        #    init sequence is resent on the new handle, then frames resume.
        opens_before = factory.opens
        first.fail_frames = True
        _wait_until(lambda: factory.opens > opens_before)
        second = factory.current
        assert second is not first
        assert first.closed
        assert second.commands()[:2] == [_DIS, _LIG]
        _wait_until(lambda: second.frames() > 0)

        # 4. The device disappears (unplug): the panel drops out ...
        failed_device = second
        failed_device.fail_frames = True
        factory.available = False
        sink_clock.advance(60)  # let the reopen attempt run and fail
        _wait_until(lambda: sink.state().state != SinkState.CONNECTED, timeout=5.0)
        assert runner.status()["panel"]["state"] == str(SinkState.DISCONNECTED)

        # ... and reappears: the runner reconnects (a counted reconnect), resends
        # the init sequence and resumes frames — without the process exiting.
        factory.available = True
        sink_clock.advance(60)
        _wait_until(lambda: runner.status()["panel"]["reconnects"] >= 1)
        third = factory.current
        assert third.commands()[:2] == [_DIS, _LIG]
        _wait_until(lambda: third.frames() > 0)

        final = runner.status()
        assert final["panel"]["state"] == str(SinkState.CONNECTED)
        assert final["panel"]["reconnects"] >= 1
        assert runner.state == "running"  # the process never exited
        assert runner.status()["frames"]["sent"] > 0
    finally:
        runner.stop()

    assert runner.state == "stopped"


def test_the_runner_reports_the_panel_sink_it_is_using(runtime: Path) -> None:
    factory = DeviceFactory()
    sink = HidSink(open_device=factory, clock=FakeClock())
    runner = Runner(Config(), sink=sink, frame_interval_s=0.05)
    runner.start()
    try:
        panel = runner.status()["panel"]
        assert panel["sink"] == "hid"
        assert panel["state"] == str(SinkState.DISCONNECTED)
        assert panel["reconnects"] == 0  # never connected, so no reconnects
        assert panel["message"] == "waiting for panel"
    finally:
        runner.stop()
