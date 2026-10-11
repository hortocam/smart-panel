"""Bundled fonts, resolution by name or path, and a size cache (T034).

DejaVu Sans (and Bold) are bundled under ``smart_panel/assets/fonts`` and cover
Latin plus a wide range of symbols; a user may point to any other TTF by path.
A missing glyph draws the font's replacement box and never raises; a missing
font file falls back along the configured chain and emits a ``ctx.notify``-style
warning that ``status`` lists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

BUNDLED_FONTS: dict[str, str] = {
    "DejaVuSans": "DejaVuSans.ttf",
    "DejaVuSans-Bold": "DejaVuSans-Bold.ttf",
}

DEFAULT_FALLBACK: tuple[str, ...] = ("DejaVuSans", "DejaVuSans-Bold")


def bundled_font_path(name: str) -> Path | None:
    """Return the path of a bundled font by name, or ``None`` if unknown/absent."""
    filename = BUNDLED_FONTS.get(name)
    if filename is None:
        return None
    path = FONT_DIR / filename
    return path if path.is_file() else None


class FontRegistry:
    """Resolve fonts and cache ``ImageFont`` objects by (path, size).

    Args:
        fallback: Ordered names/paths tried when a requested font is missing.
        notify: Optional ``ctx.notify``-style callback that receives a
            ``{"event": "font_missing", ...}`` dict.
    """

    def __init__(
        self,
        fallback: Iterable[str] = DEFAULT_FALLBACK,
        notify: Callable[[Any], None] | None = None,
    ) -> None:
        self._fallback = tuple(fallback)
        self._notify = notify
        self.warnings: list[dict[str, Any]] = []
        self._cache: dict[tuple[str, int], Any] = {}

    def resolve(self, name_or_path: str | Path) -> Path | None:
        """Resolve ``name_or_path`` to an existing font file, or ``None``."""
        candidate = Path(str(name_or_path))
        if candidate.is_file():
            return candidate
        bundled = bundled_font_path(str(name_or_path))
        if bundled is not None:
            return bundled
        for entry in self._fallback:
            path = bundled_font_path(entry)
            if path is None:
                fallback_path = Path(str(entry))
                path = fallback_path if fallback_path.is_file() else None
            if path is not None:
                self._warn(name_or_path, path)
                return path
        self._warn(name_or_path, None)
        return None

    def get(self, name_or_path: str | Path, size: int) -> Any:
        """Return a cached ``ImageFont`` for ``name_or_path`` at ``size``."""
        path = self.resolve(name_or_path)
        key = (str(path) if path is not None else "<default>", int(size))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        font = self._load(path, int(size))
        self._cache[key] = font
        return font

    def _load(self, path: Path | None, size: int) -> Any:
        if path is None:
            return _default_font(size)
        return ImageFont.truetype(str(path), size=size)

    def _warn(self, requested: str | Path, fallback: Path | None) -> None:
        event = {
            "event": "font_missing",
            "font": str(requested),
            "fallback": str(fallback) if fallback is not None else None,
        }
        self.warnings.append(event)
        if self._notify is not None:
            self._notify(event)


def _default_font(size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow without the size argument
        return ImageFont.load_default()
