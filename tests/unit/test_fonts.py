"""Unit tests for ``smart_panel.core.fonts`` (T034).

Fonts are resolved by bundled name or TTF path with a configurable fallback
chain, cached by (path, size), and a missing glyph (emoji, CJK) must draw the
replacement box without raising. A missing font file falls back along the chain
and emits a ``ctx.notify``-style warning that ``status`` can list.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from smart_panel.core.fonts import BUNDLED_FONTS, FontRegistry, bundled_font_path


def test_bundled_fonts_are_present() -> None:
    assert set(BUNDLED_FONTS) >= {"DejaVuSans", "DejaVuSans-Bold"}
    for name in ("DejaVuSans", "DejaVuSans-Bold"):
        path = bundled_font_path(name)
        assert path is not None
        assert Path(path).is_file()


def test_lookup_by_name_is_cached_by_path_and_size() -> None:
    registry = FontRegistry()
    first = registry.get("DejaVuSans", 24)
    second = registry.get("DejaVuSans", 24)
    third = registry.get("DejaVuSans", 25)
    assert first is second
    assert first is not third


def test_lookup_by_explicit_path() -> None:
    registry = FontRegistry()
    path = bundled_font_path("DejaVuSans-Bold")
    assert path is not None
    by_name = registry.get("DejaVuSans", 24)
    by_path = registry.get(str(path), 24)
    assert by_path is not by_name


def test_missing_font_falls_back_along_the_chain_and_warns() -> None:
    events: list[dict[str, object]] = []
    registry = FontRegistry(fallback=("DejaVuSans",), notify=events.append)
    font = registry.get("/no/such/font.ttf", 20)
    assert font is not None
    assert any(event.get("event") == "font_missing" for event in events)
    assert registry.warnings


def test_custom_fallback_chain_is_honoured() -> None:
    registry = FontRegistry(fallback=("DejaVuSans-Bold",))
    fallback = bundled_font_path("DejaVuSans-Bold")
    assert fallback is not None
    resolved = registry.resolve("UnknownFont")
    assert resolved == Path(fallback)


def test_missing_glyph_and_non_latin_strings_do_not_raise() -> None:
    registry = FontRegistry()
    font = registry.get("DejaVuSans", 24)
    image = Image.new("RGB", (240, 96), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    # A CJK string and an emoji code point; the bundled font has no glyph for
    # either, so the replacement box is drawn instead of raising.
    draw.text((0, 0), "\u4f60\u597d \U0001F642 world", font=font, fill=(255, 255, 255))


def test_last_resort_font_is_returned_when_the_chain_is_empty() -> None:
    registry = FontRegistry(fallback=())
    assert registry.get("/no/such/font.ttf", 18) is not None
