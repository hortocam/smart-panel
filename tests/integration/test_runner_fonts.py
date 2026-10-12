"""Runner font wiring regressions: the composed canvas draws text at the size
the ``text`` settings ask for (T051/T034).

The runner used to build its ``FrameContext`` without a ``fonts`` argument, so
``FrameContext.fonts`` stayed ``None`` and ``smart_panel.plugins.text._font``
took its fallback branch — ``ImageFont.load_default()``, which ignores both the
requested name and the size. The ``text`` settings ``font`` and ``size`` were
therefore silently ignored at runtime: ``{"size": 120}`` rendered ~8 px text,
and the two bundled fonts under ``smart_panel/assets/fonts`` were never loaded.
``contracts/plugin-sdk.md`` specifies ``fonts.get(name_or_path, size)`` "with
the configured fallback chain", so a ``None`` ``fonts`` also contradicts the
render contract.

These tests drive a real :class:`~smart_panel.core.runner.Runner` (threads, lock
and control server) with no panel attached, and assert on the **size** of the
drawn glyphs in the composed canvas fetched through ``Runner.preview_png`` — the
implementation behind the ``preview`` command — not merely that the canvas is
non-blank. ``ImageFont.load_default()`` renders "initial" 8 px tall; the glyphs
below are asserted against the same font resolved at the requested size.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.core import paths
from smart_panel.core.fonts import FontRegistry
from smart_panel.core.plugins import PluginLoader
from smart_panel.core.runner import Runner
from smart_panel.plugins.text import TextPlugin
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Manifest, Plugin

CANVAS = (1920, 462)
SIZE_PX = 120
TEXT = "initial"

# ``ImageFont.load_default()`` renders ``TEXT`` ~8 px tall; anything drawn at the
# configured 120 px is far above this floor, so it cannot be the fallback font.
DEFAULT_FONT_HEIGHT = 8
MIN_EXPECTED_HEIGHT = 60


class _FontProbe(Plugin):
    """A region plugin that records the ``FrameContext.fonts`` it is handed."""

    manifest = Manifest(name="fontprobe", version="0.1.0", capabilities=frozenset({"region"}))

    def __init__(self, ctx: object, settings: dict) -> None:
        super().__init__(ctx, settings)
        self.seen_fonts: object = None

    def render(self, frame: FrameContext) -> RenderImage:
        self.seen_fonts = getattr(frame, "fonts", None)
        size = (int(frame.size[0]), int(frame.size[1]))
        return RenderImage(PILImage.new("RGBA", size, (0, 0, 0, 255)))


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp base: AF_UNIX socket paths are capped near 108 bytes."""
    base = Path(tempfile.mkdtemp(prefix="fonts-", dir="/tmp"))
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


def _config(font: str, *, plugin: str = "text") -> dict:
    return {
        "display": {"fps": 15, "brightness": 50, "sink": "null", "min_text_px": 24},
        "layout": {
            "regions": [
                {
                    "name": "main",
                    "x": 0,
                    "y": 0,
                    "w": CANVAS[0],
                    "h": CANVAS[1],
                    "instance": plugin,
                }
            ],
            "overlays": [],
        },
        "plugins": {
            plugin: {
                "plugin": plugin,
                "enabled": True,
                "settings": {"text": TEXT, "size": SIZE_PX, "font": font},
            }
        },
    }


def _start(runtime: Path, tmp_path: Path, config: dict, *, plugin: type[Plugin]):
    paths.config_file().write_text(json.dumps(config), encoding="utf-8")
    loader = PluginLoader(builtins={plugin.manifest.name: plugin}, home=tmp_path / "plugins")
    runner = Runner(loader=loader, frame_interval_s=0.02)
    runner.start()
    return runner


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("the composed canvas never carried region content before the deadline")


def _canvas_ink_bbox(runner: Runner) -> tuple[int, int, int, int] | None:
    """The bounding box of the drawn (bright) pixels in the composed canvas.

    Returns ``None`` while the canvas is still blank (the runner's first compose
    pass may publish the bare background before the region worker has an image).
    """
    png = base64.b64decode(runner.preview_png())
    image = PILImage.open(io.BytesIO(png)).convert("L")
    ink = image.point(lambda p: 255 if p > 128 else 0)
    return ink.getbbox()


def _content_bbox(runner: Runner) -> tuple[int, int, int, int]:
    boxes: list[tuple[int, int, int, int]] = []

    def ready() -> bool:
        box = _canvas_ink_bbox(runner)
        if box is not None:
            boxes.append(box)
            return True
        return False

    _wait_for(ready)
    return boxes[-1]


def _expected_ink() -> tuple[int, int]:
    """The (width, height) of the glyphs' ink at the configured size.

    Rendered independently through the same font resolver and measured with the
    same threshold as the canvas, so it is an apples-to-apples reference (a plain
    ``textbbox`` includes the side bearings either side of the ink).
    """
    font = FontRegistry().get("DejaVuSans", SIZE_PX)
    reference = PILImage.new("L", (CANVAS[0], CANVAS[1]), 0)
    ImageDraw.Draw(reference).text((20, 20), TEXT, font=font, fill=255)
    ink = reference.point(lambda p: 255 if p > 128 else 0)
    box = ink.getbbox()
    assert box is not None
    return (box[2] - box[0], box[3] - box[1])


def _assert_drawn_at_configured_size(box: tuple[int, int, int, int]) -> None:
    height = box[3] - box[1]
    width = box[2] - box[0]
    expected_w, expected_h = _expected_ink()
    assert height >= MIN_EXPECTED_HEIGHT, (
        f"glyphs are {height} px tall, not the configured {SIZE_PX} px "
        f"(the ~{DEFAULT_FONT_HEIGHT} px default font means the font/size settings were ignored)"
    )
    # And they match the exact ink metrics of the font resolved at the requested size.
    assert abs(height - expected_h) <= 3, f"glyph height {height} px != {expected_h} px expected"
    assert abs(width - expected_w) <= 3, f"glyph width {width} px != {expected_w} px expected"


def _font_missing_events(runner: Runner) -> list[dict]:
    log = runner.log
    return [event for event in log.recent() if event.get("event") == "font_missing"]


def _font_missing_in_status(runner: Runner) -> bool:
    """T034: the runner surfaces the registry's warning through ``status``."""
    return any(event.get("event") == "font_missing" for event in runner.status()["errors"])


def test_the_runner_hands_the_plugin_a_font_registry(runtime: Path, tmp_path: Path) -> None:
    runner = _start(runtime, tmp_path, _config("DejaVuSans", plugin="fontprobe"), plugin=_FontProbe)
    try:
        probe = runner._instances["fontprobe"]  # noqa: SLF001 - inspect the live instance
        _wait_for(lambda: probe.seen_fonts is not None)

        # contracts/plugin-sdk.md: ``fonts.get(name_or_path, size)`` with the
        # configured fallback chain; a ``None`` fonts breaks that contract.
        assert isinstance(probe.seen_fonts, FontRegistry), (
            f"the FrameContext carried fonts={probe.seen_fonts!r}, not a FontRegistry"
        )
    finally:
        runner.stop()


def test_text_renders_at_the_configured_font_size(runtime: Path, tmp_path: Path) -> None:
    runner = _start(runtime, tmp_path, _config("DejaVuSans"), plugin=TextPlugin)
    try:
        box = _content_bbox(runner)
        _assert_drawn_at_configured_size(box)
    finally:
        runner.stop()


def test_an_unknown_font_name_is_not_downgraded_to_the_default_size(
    runtime: Path, tmp_path: Path
) -> None:
    runner = _start(runtime, tmp_path, _config("NoSuchFont"), plugin=TextPlugin)
    try:
        box = _content_bbox(runner)
        # The fallback chain resolves the unknown name to a bundled font *at the
        # requested size*; the ~8 px default is no longer reached here.
        _assert_drawn_at_configured_size(box)
        assert _font_missing_events(runner), "an unknown font name must warn (font_missing)"
        # T034: that warning is what ``status`` lists.
        assert _font_missing_in_status(runner), "the font warning must surface in status.errors"
    finally:
        runner.stop()


def test_a_missing_font_path_falls_back_and_warns(runtime: Path, tmp_path: Path) -> None:
    runner = _start(runtime, tmp_path, _config("/no/such/font.ttf"), plugin=TextPlugin)
    try:
        box = _content_bbox(runner)
        _assert_drawn_at_configured_size(box)
        assert _font_missing_events(runner), "a missing font path must warn (font_missing)"
    finally:
        runner.stop()
