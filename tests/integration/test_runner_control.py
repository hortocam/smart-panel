"""Runner regressions found at the wave-2 review (T051).

F1 — ``frame.size`` for a plugin must be its **region** size. The render
contract (``contracts/plugin-sdk.md``) says an ``Image`` result is "exactly
``frame.size``", and :class:`~smart_panel.core.workers.RenderWorker` validates a
result against that region size. The compose loop must therefore hand every
worker a frame sized to its own region rather than the whole canvas, or a
contract-conformant plugin is degraded on every frame.

F2 — a failed persist must leave the last good configuration in effect
(FR-006). ``display.fps`` / ``display.brightness`` are applied live before the
config is written, so a write failure used to leave the sink and the governor
drifted from the config on disk. The live apply is rolled back on a save
failure.

These tests drive a real :class:`~smart_panel.core.runner.Runner` (threads,
lock and control server) with no panel attached, so they need the short-path
runtime environment the panel-loss test also sets up.
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
from smart_panel.core.config import Config
from smart_panel.core.plugins import PluginLoader
from smart_panel.core.runner import Runner
from smart_panel.core.sinks import NullSink
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Manifest, Plugin

REGION = (300, 200)


class _ConformantRegion(Plugin):
    """A contract-conformant region plugin: renders exactly ``frame.size``."""

    manifest = Manifest(name="conformant", version="0.1.0", capabilities=frozenset({"region"}))

    def __init__(self, ctx: object, settings: dict) -> None:
        super().__init__(ctx, settings)
        self.sizes: list[tuple[int, int]] = []

    def render(self, frame: FrameContext) -> RenderImage:
        size = (int(frame.size[0]), int(frame.size[1]))
        self.sizes.append(size)
        return RenderImage(PILImage.new("RGBA", size, (0, 0, 0, 255)))


class _RecordingSink(NullSink):
    """A null sink that records every brightness it is asked to apply."""

    def __init__(self) -> None:
        super().__init__()
        self.applied: list[int] = []

    def set_brightness(self, n: int) -> None:
        super().set_brightness(n)
        self.applied.append(n)


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
    base = Path(tempfile.mkdtemp(prefix="rctl-", dir="/tmp"))
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


def _region_config() -> dict:
    return {
        "display": {"fps": 15, "brightness": 50, "sink": "null", "min_text_px": 24},
        "layout": {
            "regions": [
                {
                    "name": "main",
                    "x": 0,
                    "y": 0,
                    "w": REGION[0],
                    "h": REGION[1],
                    "instance": "conformant",
                }
            ],
            "overlays": [],
        },
        "plugins": {"conformant": {"plugin": "conformant", "enabled": True, "settings": {}}},
    }


def test_a_plugin_is_handed_a_frame_sized_to_its_region(runtime: Path, tmp_path: Path) -> None:
    # The runner reads its config from disk on start, so seed <home>/config.json.
    config_file = paths.config_file()
    config_file.write_text(json.dumps(_region_config()), encoding="utf-8")

    loader = PluginLoader(builtins={"conformant": _ConformantRegion}, home=tmp_path / "plugins")
    runner = Runner(loader=loader, frame_interval_s=0.02)
    runner.start()
    try:
        plugin = runner._instances["conformant"]  # noqa: SLF001 - inspect the live instance
        _wait_until(lambda: plugin.sizes)

        # The contract says an Image result is exactly frame.size; a plugin that
        # obeys it must be handed its region, not the 1920x462 canvas.
        assert plugin.sizes[-1] == REGION
        _wait_until(lambda: runner.status()["plugins"]["conformant"]["state"] == "ok")
    finally:
        runner.stop()


def test_a_failed_persist_keeps_the_last_good_configuration_live(
    runtime: Path, tmp_path: Path
) -> None:
    # A config path whose parent is a file: load() falls back to defaults and
    # every save() raises OSError (a realistic persist failure).
    marker = tmp_path / "not-a-directory"
    marker.write_text("x", encoding="utf-8")

    sink = _RecordingSink()
    runner = Runner(Config(path=marker / "config.json"), sink=sink, frame_interval_s=5.0)
    runner.start()
    try:
        assert runner.status()["frames"]["target_fps"] == 15
        assert runner.brightness() == 50

        with pytest.raises(OSError):
            runner.set_config("display.fps", 25)
        with pytest.raises(OSError):
            runner.set_brightness(80)

        # FR-006: the last good configuration is still in effect live.
        assert runner.status()["frames"]["target_fps"] == 15
        assert runner.brightness() == 50
        assert runner.config_data["display"]["fps"] == 15
        assert runner.config_data["display"]["brightness"] == 50
        # The failed brightness was applied and then rolled back, so the sink
        # does not keep the drifted value.
        assert 80 in sink.applied
        assert sink.applied[-1] == 50
    finally:
        runner.stop()
