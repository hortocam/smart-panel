"""The first frame the runner sends carries region content (T051 regression).

``RenderWorker`` renders on its own thread, so ``_compose_once`` submits a frame
to each region worker and reads ``worker.last_image`` in the *same* pass: on the
first pass no worker has produced an image yet and the compositor publishes the
bare background. ``start``/``restart`` return as soon as a frame has been
*sent*, so an immediate ``preview`` used to catch an empty canvas. On Linux the
gap between the blank frame and the first content frame is smaller than the next
CLI round-trip; on a loaded macOS CI runner it is not (observed: a fully blank
1920x462 canvas, ``max_luminance=0``).

These tests drive a real :class:`~smart_panel.core.runner.Runner` (threads, lock
and control server) with a sink that records every canvas it is handed, so they
pin *which* frame is the first one the runner sends:

* a region instance that takes a moment to render still makes the **first** sent
  frame a content frame (no bare background is ever sent);
* a region instance that raises is non-blank on the first sent frame too (the
  compositor's error badge), so a broken plugin never sends a blank frame either;
* a layout whose only instance is ``Inactive`` forever still starts (bounded),
  is genuinely empty, and ``status`` explains why;
* a layout with no region instances at all still starts and stays quiet.

No panel is attached, so a recording sink and fake plugins stand in for the
hardware.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage

from smart_panel.core import paths
from smart_panel.core.plugins import PluginLoader
from smart_panel.core.runner import Runner
from smart_panel.core.sinks import NullSink
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Inactive, Manifest, Plugin

CANVAS = (1920, 462)
REGION = (300, 200)
# Slow enough that the worker cannot finish its first render before the first
# compose pass reads ``last_image`` — the race the fix closes.
SLOW_RENDER_S = 0.15


class _SlowBrightRegion(Plugin):
    """A region plugin that paints its whole region white, one beat too late."""

    manifest = Manifest(name="slow", version="0.1.0", capabilities=frozenset({"region"}))

    def render(self, frame: FrameContext) -> RenderImage:
        time.sleep(SLOW_RENDER_S)
        size = (int(frame.size[0]), int(frame.size[1]))
        return RenderImage(PILImage.new("RGBA", size, (255, 255, 255, 255)))


class _RaisingRegion(Plugin):
    """A region plugin whose render always raises (the worker marks it degraded)."""

    manifest = Manifest(name="broken", version="0.1.0", capabilities=frozenset({"region"}))

    def render(self, frame: FrameContext) -> RenderImage:
        raise ValueError("render exploded")


class _InactiveForeverRegion(Plugin):
    """A region plugin that never draws anything."""

    manifest = Manifest(name="inactive", version="0.1.0", capabilities=frozenset({"region"}))

    def render(self, frame: FrameContext) -> Inactive:
        return Inactive()


class _RecordingSink(NullSink):
    """A null sink that keeps a copy of every canvas it is sent."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[PILImage.Image] = []

    def send(self, frame: object) -> None:
        super().send(frame)
        assert isinstance(frame, PILImage.Image)
        self.frames.append(frame.copy())


def is_non_blank(image: PILImage.Image) -> bool:
    """A frame is non-blank when it carries at least one bright pixel."""
    high = image.convert("L").getextrema()[1]  # type: ignore[index]
    return int(high) > 200


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before the deadline")


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp base: AF_UNIX socket paths are capped near 108 bytes."""
    base = Path(tempfile.mkdtemp(prefix="fframe-", dir="/tmp"))
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


def _seed(plugin: str | None) -> None:
    """Write the config the runner loads from disk on start.

    ``plugin`` names the one region instance to configure; ``None`` leaves the
    layout empty (no region instances at all).
    """
    document: dict = {
        "display": {"fps": 15, "brightness": 50, "sink": "null", "min_text_px": 24},
        "layout": {"regions": [], "overlays": []},
        "plugins": {},
    }
    if plugin is not None:
        document["layout"]["regions"] = [
            {
                "name": "main",
                "x": 0,
                "y": 0,
                "w": REGION[0],
                "h": REGION[1],
                "instance": "main",
            }
        ]
        document["plugins"] = {"main": {"plugin": plugin, "enabled": True, "settings": {}}}
    paths.config_file().write_text(json.dumps(document), encoding="utf-8")


def _start(plugin: str | None, tmp_path: Path, *, name: str) -> tuple[Runner, _RecordingSink]:
    _seed(plugin)
    sink = _RecordingSink()
    loader = (
        PluginLoader(builtins={}, home=tmp_path / "plugins")
        if plugin is None
        else PluginLoader(builtins={plugin: _PLUGINS[plugin]}, home=tmp_path / "plugins")
    )
    runner = Runner(loader=loader, sink=sink, frame_interval_s=0.02)
    runner.start()
    return runner, sink


def _events(status: dict, event: str) -> list[dict]:
    return [entry for entry in status["errors"] if entry.get("event") == event]


def test_the_first_sent_frame_carries_region_content(runtime: Path, tmp_path: Path) -> None:
    runner, sink = _start("slow", tmp_path, name="slow")
    try:
        _wait_until(lambda: sink.frames)
        first = sink.frames[0]
        assert first.size == CANVAS
        assert is_non_blank(first), (
            "the runner's first frame is the bare background: the compositor "
            "published before any region worker had produced an image"
        )
    finally:
        runner.stop()


def test_a_raising_region_is_non_blank_on_the_first_frame(
    runtime: Path, tmp_path: Path
) -> None:
    # A degraded instance draws the compositor's error badge, whose white marker
    # pixels are bright: the first frame must carry it rather than the bare
    # background, or `start` would return a blank canvas for a broken plugin.
    runner, sink = _start("broken", tmp_path, name="broken")
    try:
        _wait_until(lambda: sink.frames)
        assert is_non_blank(sink.frames[0])
        status = runner.status()
        assert status["plugins"]["main"]["state"] == "degraded"
        assert _events(status, "first_frame_without_content") == []
    finally:
        runner.stop()


def test_an_inactive_only_layout_still_starts_and_explains_the_empty_canvas(
    runtime: Path, tmp_path: Path
) -> None:
    runner, sink = _start("inactive", tmp_path, name="inactive")
    try:
        # Bounded: the instance never draws, so waiting for content would hang
        # forever. The runner starts anyway ...
        _wait_until(lambda: sink.frames)
        assert not is_non_blank(sink.frames[0])

        # ... and `status` says why the canvas is empty: the instance finished
        # its first render, produced nothing, and the canvas is the background.
        status = runner.status()
        assert status["state"] == "running"
        assert status["plugins"]["main"]["state"] == "ok"
        notes = _events(status, "first_frame_without_content")
        assert notes, f"status does not explain the empty canvas: {status['errors']}"
        assert notes[-1]["plugins"] == {"main": "ok"}
        assert "background" in notes[-1]["message"]
    finally:
        runner.stop()


def test_a_layout_with_no_region_instances_still_starts_quietly(
    runtime: Path, tmp_path: Path
) -> None:
    # Nothing to wait for and nothing to explain: the runner must not hold the
    # first frame back, and must not log an explanation for an empty canvas that
    # has no instances behind it.
    runner, sink = _start(None, tmp_path, name="empty")
    try:
        _wait_until(lambda: sink.frames)
        assert not is_non_blank(sink.frames[0])
        assert _events(runner.status(), "first_frame_without_content") == []
    finally:
        runner.stop()


_PLUGINS = {
    "slow": _SlowBrightRegion,
    "broken": _RaisingRegion,
    "inactive": _InactiveForeverRegion,
}
