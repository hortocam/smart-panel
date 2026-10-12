"""Contract tests for the SDK testing harness ``smart_panel.sdk.testing`` (T031).

The harness lets a plugin author render and drive a plugin without a panel, and
``assert_conforms`` is the standard conformance suite every built-in and sample
plugin runs in CI (contract ``plugin-sdk.md``): manifest valid, settings
validate, commands declared, render returns allowed types at the exact size,
``stop`` is prompt, and no forbidden imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.sdk.plugin import (
    Command,
    Manifest,
    Plugin,
    Unchanged,
)
from smart_panel.sdk.plugin import (
    Image as RenderImage,
)
from smart_panel.sdk.settings import Setting
from smart_panel.sdk.testing import FakeClock, PluginHarness, assert_conforms


class SamplePlugin(Plugin):
    """A tiny region plugin used to exercise the harness itself."""

    manifest = Manifest(
        name="sample",
        version="1.0.0",
        sdk_version="1",
        summary="a sample region plugin",
        capabilities=frozenset({"region"}),
        settings=(Setting(name="text", type="str", default="hi", description=""),),
        commands=(
            Command(
                name="set",
                summary="set the text",
                args=(Setting(name="text", type="str", default="", description=""),),
            ),
        ),
    )

    def render(self, frame: object) -> object:
        size = frame.size  # type: ignore[attr-defined]
        return RenderImage(PILImage.new("RGBA", size, (0, 0, 0, 255)))

    def handle_command(self, name: str, args: dict) -> dict:
        if name == "set":
            self.settings["text"] = args.get("text", "")
            return {"text": self.settings["text"]}
        return super().handle_command(name, args)

    def describe_state(self) -> dict:
        return {"text": self.settings["text"]}


def test_harness_renders_at_the_region_size() -> None:
    harness = PluginHarness(SamplePlugin, {}, size=(100, 80))
    image = harness.render()
    assert image is not None
    assert image.size == (100, 80)
    harness.stop()


def test_harness_fake_clock_advances() -> None:
    clock = FakeClock(start=0.0)
    harness = PluginHarness(SamplePlugin, {}, size=(10, 10), clock=clock)
    harness.render(advance_s=5)
    assert clock.monotonic() == 5
    harness.stop()


def test_harness_command_settings_and_state() -> None:
    harness = PluginHarness(SamplePlugin, {}, size=(10, 10))
    assert harness.command("set", text="bye") == {"text": "bye"}
    harness.set_settings(text="x")
    assert harness.state() == {"text": "x"}
    harness.stop()


def test_harness_exposes_a_bus() -> None:
    harness = PluginHarness(SamplePlugin, {}, size=(10, 10))
    harness.bus.publish("push.count", 3)
    assert harness.bus.get("push.count").value == 3
    harness.stop()


def test_harness_returns_none_for_unchanged_result() -> None:
    class Static(Plugin):
        manifest = Manifest(name="static", summary="static")

        def render(self, frame: object) -> object:
            return Unchanged()

    harness = PluginHarness(Static, {}, size=(10, 10))
    assert harness.render() is None
    harness.stop()


def test_harness_resolves_fonts_at_the_requested_size() -> None:
    """The harness draws text like the runner, not at the ~8 px default font.

    ``text`` settings ``font``/``size`` must mean the same thing headless as in
    production, so the harness resolves through the runner's ``FontRegistry``
    (T034) instead of ``ImageFont.load_default()``, which ignores both the name
    and the size (a plugin asking for 120 px rendered ~8 px here while the runner
    drew 120 px, so a harness test could not catch a font/size defect).
    """

    class TextSample(Plugin):
        manifest = Manifest(name="harness-text", summary="draws text via frame.fonts")

        def render(self, frame: object) -> object:
            size = frame.size  # type: ignore[attr-defined]
            image = PILImage.new("RGBA", size, (0, 0, 0, 255))
            font = frame.fonts.get("DejaVuSans", 120)  # type: ignore[attr-defined]
            ImageDraw.Draw(image).text((0, 0), "initial", font=font, fill=(255, 255, 255, 255))
            return RenderImage(image)

    harness = PluginHarness(TextSample, {}, size=(600, 400))
    try:
        image = harness.render()
        assert image is not None
        ink = image.convert("L").point(lambda p: 255 if p > 128 else 0).getbbox()
        assert ink is not None
        height = ink[3] - ink[1]
        assert height >= 60, f"the harness drew {height} px glyphs, not the requested 120 px"
    finally:
        harness.stop()


def test_assert_conforms_passes_for_a_region_plugin() -> None:
    assert_conforms(SamplePlugin)


def test_assert_conforms_rejects_a_forbidden_core_import(tmp_path: Path) -> None:
    module_path = tmp_path / "bad_dropin.py"
    module_path.write_text(
        "from smart_panel.sdk.plugin import Plugin, Manifest\n"
        "from smart_panel.core.bus import Bus\n"
        "class Bad(Plugin):\n"
        "    manifest = Manifest(name='baddemo')\n"
    )
    spec = importlib.util.spec_from_file_location("bad_dropin_module", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)

        with pytest.raises(AssertionError):
            assert_conforms(module.Bad)
    finally:
        sys.modules.pop("bad_dropin_module", None)


def test_assert_conforms_rejects_an_invalid_manifest() -> None:
    class Bad(Plugin):
        manifest = Manifest(name="Bad Name")

    with pytest.raises(AssertionError):
        assert_conforms(Bad)
