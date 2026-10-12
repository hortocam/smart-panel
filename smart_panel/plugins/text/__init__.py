"""The built-in ``text`` plugin: static text in a region (T043).

``text`` is the reference region plugin and the first-run verification display
(US1). It draws one line of text, centred by default, on a solid background,
sized to whatever region the layout assigns it. It imports only
``smart_panel.sdk`` and ``PIL`` (FR-043); a contract test enforces that.

Settings and the ``set`` verb are pinned by ``contracts/builtin-plugins.md``:
``text`` (<= 280 chars; an empty string is a valid blank frame), ``fg``/``bg``
(colour), ``font`` (bundled name or TTF path), ``size`` (>= ``frame.min_text_px``),
``align`` (``left|center|right``), and ``set --text T`` marked ``spoolable``.
"""

from __future__ import annotations

from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Manifest,
    Plugin,
    SettingsRejected,
    Unchanged,
)
from smart_panel.sdk.plugin import (
    Image as RenderImage,
)
from smart_panel.sdk.settings import Setting

MAX_TEXT_CHARS = 280
_FLOOR_PX = 12  # ``display.min_text_px`` lower bound; the render clamps to the frame's value

_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="text",
        type="str",
        default="smart-panel",
        description="Text to display (at most 280 characters)",
    ),
    Setting(name="fg", type="color", default="#FFFFFF", description="Foreground colour"),
    Setting(name="bg", type="color", default="#000000", description="Background colour"),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="size",
        type="int",
        default=48,
        description="Text size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
    Setting(
        name="align",
        type="enum",
        default="center",
        description="Horizontal alignment",
        choices=["left", "center", "right"],
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="set",
        summary="Set the displayed text",
        args=(
            Setting(
                name="text",
                type="str",
                default="",
                description="Text to display (at most 280 characters)",
            ),
        ),
        spoolable=True,
    ),
)

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


class TextPlugin(Plugin):
    """Draw a single line of text, sized to the region it renders into."""

    manifest = Manifest(
        name="text",
        version="0.1.0",
        summary="Static text, used for first-run verification",
        capabilities=frozenset({"region"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
    )

    # ------------------------------------------------------------ lifecycle

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Accept a settings change, refusing text over the 280-char limit."""
        text = settings.get("text")
        if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
            raise SettingsRejected(
                f"text must be at most {MAX_TEXT_CHARS} characters", setting="text"
            )
        self.settings = dict(settings)

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Unchanged | RenderImage:
        """Render the text into a ``frame.size`` image (contract: exact size)."""
        width, height = int(frame.size[0]), int(frame.size[1])
        fg = _rgba(self.settings.get("fg", "#FFFFFF"))
        bg = _rgba(self.settings.get("bg", "#000000"))
        image = PILImage.new("RGBA", (width, height), bg)
        draw = ImageDraw.Draw(image)

        size_px = int(self.settings.get("size", 48))
        floor = int(getattr(frame, "min_text_px", 0) or 0)
        if floor > 0:
            size_px = max(size_px, floor)
        font = _font(frame, str(self.settings.get("font", "DejaVuSans")), size_px)

        text = str(self.settings.get("text", ""))
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_w = right - left
        text_h = bottom - top
        align = str(self.settings.get("align", "center"))
        if align == "left":
            x = 0
        elif align == "right":
            x = width - text_w
        else:
            x = (width - text_w) / 2
        y = (height - text_h) / 2 - top
        draw.text((x, y), text, font=font, fill=fg)
        return RenderImage(image)

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one CLI verb: ``set --text T`` (spoolable)."""
        if name != "set":
            raise CommandError("invalid_input", f"unknown command {name!r}")
        text = args.get("text")
        if not isinstance(text, str):
            raise CommandError("invalid_input", "set needs a 'text' string")
        # An empty string is a valid value, not an input error: it draws a
        # background-only blank frame. This is deliberate and pinned by
        # ``contracts/builtin-plugins.md`` (``text``) — ``set --text ""`` is the
        # only way to blank a region (the plugin has no ``clear`` verb), so it is
        # neither refused nor clamped. Do not add empty-text validation here.
        if len(text) > MAX_TEXT_CHARS:
            raise CommandError(
                "invalid_input", f"text must be at most {MAX_TEXT_CHARS} characters"
            )
        self.settings["text"] = text
        log = getattr(self.ctx, "log", None)
        if log is not None:
            # Records the applied value so ``logs`` shows spool replay order.
            log.info("text_set", text=text)
        return {"text": text}

    def describe_state(self) -> dict[str, Any]:
        """The current text, for ``status``/``plugin describe`` (never a secret)."""
        return {"text": str(self.settings.get("text", ""))}


def _font(frame: Any, name: str, size_px: int) -> Any:
    """Resolve a font through ``frame.fonts`` (the harness and runner provide one)."""
    fonts = getattr(frame, "fonts", None)
    if fonts is not None and hasattr(fonts, "get"):
        return fonts.get(name, size_px)
    from PIL import ImageFont

    return ImageFont.load_default()
