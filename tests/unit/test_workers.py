"""Unit tests for ``smart_panel.core.workers`` (T029).

Each plugin instance gets one render worker thread with a latest-result slot.
A render that overruns the soft deadline (250 ms) is logged; one that overruns
the hard deadline (2 s) marks the instance ``stalled`` and keeps the last good
image until the render returns. A render that raises, returns the wrong type or
returns the wrong size marks the instance ``degraded`` with a ``last_error`` and
keeps the previous image.
"""

from __future__ import annotations

import threading
import time

from PIL import Image as PILImage

from smart_panel.core.workers import InstanceState, RenderWorker
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.plugin import Image as RenderImage


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def debug(self, event: str, **fields: object) -> None:
        self.events.append(("debug", event))

    def info(self, event: str, **fields: object) -> None:
        self.events.append(("info", event))

    def warning(self, event: str, **fields: object) -> None:
        self.events.append(("warning", event))

    def error(self, event: str, **fields: object) -> None:
        self.events.append(("error", event))


class _Stub:
    def __init__(self, result: object = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls = 0

    def render(self, frame: FrameContext) -> object:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def _frame() -> FrameContext:
    return FrameContext(size=(100, 80), now=0.0, wall=0.0, frame_index=1)


def _image(size: tuple[int, int] = (100, 80)) -> PILImage.Image:
    return PILImage.new("RGBA", size, (0, 0, 0, 255))


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not met before the deadline")


def test_initial_state_is_starting(fake_clock) -> None:
    worker = RenderWorker(_Stub(result=RenderImage(_image())), clock=fake_clock, size=(100, 80))
    assert worker.state == InstanceState.STARTING
    worker.stop()


def test_a_good_render_sets_ok_and_stores_the_image(fake_clock) -> None:
    worker = RenderWorker(_Stub(result=RenderImage(_image())), clock=fake_clock, size=(100, 80))
    try:
        worker.submit(_frame())
        assert worker.wait_idle(2)
        assert worker.state == InstanceState.OK
        assert worker.last_image is not None
        assert worker.last_image.size == (100, 80)
    finally:
        worker.stop()


def test_an_exception_marks_degraded_and_keeps_the_last_image(fake_clock) -> None:
    stub = _Stub(result=RenderImage(_image()))
    worker = RenderWorker(stub, clock=fake_clock, size=(100, 80))
    try:
        worker.submit(_frame())
        assert worker.wait_idle(2)
        first = worker.last_image
        assert first is not None

        stub.result = None
        stub.exc = ValueError("boom")
        worker.submit(_frame())
        assert worker.wait_idle(2)
        assert worker.state == InstanceState.DEGRADED
        assert worker.last_error == "boom"
        assert worker.last_image is first
    finally:
        worker.stop()


def test_a_wrong_size_marks_degraded(fake_clock) -> None:
    worker = RenderWorker(
        _Stub(result=RenderImage(_image((10, 10)))), clock=fake_clock, size=(100, 80)
    )
    try:
        worker.submit(_frame())
        assert worker.wait_idle(2)
        assert worker.state == InstanceState.DEGRADED
        assert "size" in (worker.last_error or "")
    finally:
        worker.stop()


def test_a_wrong_type_marks_degraded(fake_clock) -> None:
    worker = RenderWorker(_Stub(result="not a render result"), clock=fake_clock, size=(100, 80))
    try:
        worker.submit(_frame())
        assert worker.wait_idle(2)
        assert worker.state == InstanceState.DEGRADED
    finally:
        worker.stop()


def test_soft_deadline_is_logged_and_hard_deadline_stalls(fake_clock) -> None:
    release = threading.Event()

    class Slow:
        def render(self, frame: FrameContext) -> object:
            release.wait(2)
            return RenderImage(_image())

    log = _Log()
    worker = RenderWorker(Slow(), clock=fake_clock, size=(100, 80), log=log)
    try:
        worker.submit(_frame())
        _wait_until(lambda: worker.in_flight)

        fake_clock.advance(0.3)
        assert worker.check_deadlines() is False
        assert any(level == "warning" for level, _ in log.events)

        fake_clock.advance(2.0)
        assert worker.check_deadlines() is True
        assert worker.state == InstanceState.STALLED
        # ``status`` reports a stalled instance "with the last error" (T111), so
        # the hard-deadline overrun records why it is stalled, exactly as a raised
        # render records its message.
        assert "hard deadline" in (worker.last_error or "")

        release.set()
        assert worker.wait_idle(2)
        assert worker.state == InstanceState.OK
        assert worker.last_error is None, "recovering from a stall clears the error"
    finally:
        release.set()
        worker.stop()


def test_disable_and_failed_states(fake_clock) -> None:
    worker = RenderWorker(_Stub(result=RenderImage(_image())), clock=fake_clock, size=(100, 80))
    try:
        worker.disable()
        assert worker.state == InstanceState.DISABLED
        worker.submit(_frame())
        assert worker.wait_idle(1)
        assert worker.last_image is None
    finally:
        worker.stop()

    failed = RenderWorker(_Stub(), clock=fake_clock, size=(100, 80))
    try:
        failed.mark_failed("no manifest")
        assert failed.state == InstanceState.FAILED
        assert failed.last_error == "no manifest"
    finally:
        failed.stop()
