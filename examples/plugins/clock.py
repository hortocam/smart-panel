"""A sample third-party plugin: a clock, shipped as a drop-in (T113, US6).

This file is the worked example for ``docs/plugin-authoring.md`` and the plugin
that quickstart **Scenario G** copies into ``<home>/plugins/``::

    cp examples/plugins/clock.py "$SMART_PANEL_HOME/plugins/"

It is deliberately ordinary — it uses **only** ``smart_panel.sdk`` (plus the
stdlib and ``PIL``), like any built-in (FR-043), and it declares everything the
SDK contract asks for:

* a :class:`~smart_panel.sdk.plugin.Manifest` with the ``region`` capability and
  ``animated = True`` (a clock changes every second, so it is rendered every
  frame rather than cached);
* declared settings — ``format`` (a ``strftime`` pattern), ``fg``/``bg``
  colours, ``font`` and ``size`` — so ``plugin describe`` can show their types,
  ranges and defaults and the runner can validate them before they reach the
  plugin (FR-046);
* a ``crash`` verb that makes ``render`` raise, so a test or the quickstart can
  prove plugin isolation: the failing region is reported ``degraded`` in
  ``status`` while every other region keeps rendering (FR-045, SC-009). A
  matching ``reset`` verb restores it.

Its ``render`` is a pure function of the settings and the frame: the time comes
from ``frame.wall`` (never ``time.time()`` or a frame count), so a fake clock in
tests drives it deterministically and dropped frames cannot make it jitter.
"""

from __future__ import annotations

import time
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Manifest,
    Plugin,
    Unchanged,
)
from smart_panel.sdk.plugin import (
    Image as RenderImage,
)
from smart_panel.sdk.settings import Setting

__all__ = ["ClockPlugin"]

# The CSS-style colour names the SDK's ``color`` setting accepts, as RGBA.
_NAMED_COLOURS: dict[str, tuple[int, int, int, int]] = {
    "black": (0, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "red": (255, 0, 0, 255),
    "green": (0, 128, 0, 255),
    "blue": (0, 0, 255, 255),
    "yellow": (255, 255, 0, 255),
    "orange": (255, 165, 0, 255),
    "purple": (128, 0, 128, 255),
    "grey": (128, 128, 128, 255),
    "gray": (128, 128, 128, 255),
    "cyan": (0, 255, 255, 255),
    "magenta": (255, 0, 255, 255),
    "transparent": (0, 0, 0, 0),
}

# ``display.min_text_px`` lower bound; the render clamps to the frame's value.
_FLOOR_PX = 12

_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="format",
        type="str",
        default="%H:%M:%S",
        description="A strftime(3) pattern, for example '%H:%M' or '%a %d %b'",
    ),
    Setting(name="fg", type="color", default="#FFFFFF", description="Text colour"),
    Setting(name="bg", type="color", default="#101418", description="Background colour"),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="size",
        type="int",
        default=96,
        description="Text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="crash",
        summary="Make render() raise, to demonstrate plugin isolation",
    ),
    Command(
        name="reset",
        summary="Clear a previous crash so render() draws again",
    ),
)


def _rgba(value: Any) -> tuple[int, int, int, int]:
    """Parse a validated colour value as RGBA (falling back to opaque white)."""
    text = str(value).strip()
    if text.startswith("#"):
        digits = text[1:]
        try:
            if len(digits) == 6:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    255,
                )
            if len(digits) == 8:
                return (
                    int(digits[0:2], 16),
                    int(digits[2:4], 16),
                    int(digits[4:6], 16),
                    int(digits[6:8], 16),
                )
        except ValueError:
            pass
    return _NAMED_COLOURS.get(text.lower(), (255, 255, 255, 255))


def _font(frame: Any, name: str, size_px: int) -> Any:
    """Resolve a font through ``frame.fonts`` (the harness and runner provide one)."""
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size_px)
    from PIL import ImageFont

    return ImageFont.load_default()


class ClockPlugin(Plugin):
    """Draw the wall-clock time, formatted by the ``format`` setting."""

    manifest = Manifest(
        name="clock",
        version="1.0.0",
        summary="A sample third-party clock plugin (drop-in)",
        capabilities=frozenset({"region"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
        animated=True,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        super().__init__(ctx, settings)
        self._crashed = False

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Unchanged | RenderImage:
        """Render the current time into a ``frame.size`` image (contract: exact size).

        The time is read from ``frame.wall``, never the process clock, so the SDK
        harness's :class:`~smart_panel.sdk.testing.FakeClock` drives it and the
        render stays a pure function of the frame (contract ``plugin-sdk.md``).
        """
        if self._crashed:
            raise RuntimeError("clock: render() failed on request (the 'crash' verb)")
        width, height = int(frame.size[0]), int(frame.size[1])
        fg = _rgba(self.settings.get("fg", "#FFFFFF"))
        bg = _rgba(self.settings.get("bg", "#101418"))
        image = PILImage.new("RGBA", (width, height), bg)
        draw = ImageDraw.Draw(image)

        size_px = int(self.settings.get("size", 96))
        floor = int(getattr(frame, "min_text_px", 0) or 0)
        if floor > 0:
            size_px = max(size_px, floor)
        font = _font(frame, str(self.settings.get("font", "DejaVuSans")), size_px)

        pattern = str(self.settings.get("format", "%H:%M:%S"))
        try:
            text = time.strftime(pattern, time.localtime(float(frame.wall)))
        except (ValueError, OSError):
            # A malformed pattern must not take down the region: draw the error.
            text = "bad format"
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        x = (width - (right - left)) / 2
        y = (height - (bottom - top)) / 2 - top
        draw.text((x, y), text, font=font, fill=fg)
        return RenderImage(image)

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one verb: ``crash`` (fail the next render) or ``reset``."""
        if name == "crash":
            self._crashed = True
            log = getattr(self.ctx, "log", None)
            if log is not None:
                log.warning("clock_crash_requested")
            return {"crashed": True}
        if name == "reset":
            self._crashed = False
            return {"crashed": False}
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def describe_state(self) -> dict[str, Any]:
        """Small health summary for ``status``/``plugin describe`` (never a secret)."""
        return {"format": str(self.settings.get("format", "%H:%M:%S")), "crashed": self._crashed}
