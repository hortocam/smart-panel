"""Bundled icons: resolution by name or PNG path, generic fallback (T105, US5).

The sidebar's icons ship under :data:`ICON_DIR` as 64 px PNGs derived from
Tabler Icons (MIT — see ``THIRD_PARTY_NOTICES.md``). A plugin asks for an icon by
its bundled name (``"mail"``) or by the path of any PNG file (FR-038), and always
gets a square image back:

* a name that is not bundled, and a path that does not exist, resolve to ``None``
  from :meth:`IconRegistry.resolve`;
* a name that resolves but whose file cannot be decoded falls back to the
  *generic* icon, and the failure is recorded in :attr:`IconRegistry.warnings`
  (and sent to the optional ``notify`` callback) so ``status`` lists it instead of
  a broken icon appearing silently.

The module mirrors :mod:`smart_panel.core.fonts`: the runner builds one registry,
hands it to every render frame as ``frame.icons`` and forwards its warnings to the
structured log, so a missing icon is visible in ``status`` exactly like a missing
font.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

__all__ = [
    "DEFAULT_ICON_SIZE",
    "GENERIC_COLOUR",
    "GENERIC_NAME",
    "ICON_DIR",
    "IconRegistry",
    "bundled_icons",
    "bundled_icon_path",
]

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
DEFAULT_ICON_SIZE = 64
GENERIC_NAME = "generic"
GENERIC_COLOUR = (120, 130, 140, 255)


def bundled_icons() -> tuple[str, ...]:
    """The names of the bundled icons (the PNG stems under :data:`ICON_DIR`)."""
    if not ICON_DIR.is_dir():
        return ()
    return tuple(sorted(path.stem for path in ICON_DIR.glob("*.png")))


def bundled_icon_path(name: str) -> Path | None:
    """The path of the bundled icon ``name``, or ``None`` when it is not bundled."""
    path = ICON_DIR / f"{name}.png"
    return path if path.is_file() else None


class IconRegistry:
    """Resolve icons and cache them by (name, size).

    Args:
        directory: Directory of bundled PNGs; defaults to :data:`ICON_DIR`.
        notify: Optional ``ctx.notify``-style callback that receives an
            ``{"event": "icon_unusable", "icon": ..., "reason": ...}`` dict.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        notify: Callable[[Any], None] | None = None,
    ) -> None:
        self._dir = Path(directory) if directory is not None else ICON_DIR
        self._notify = notify
        self.warnings: list[dict[str, Any]] = []
        self._cache: dict[tuple[str, int], tuple[Image.Image, bool]] = {}
        self._generic: dict[int, Image.Image] = {}

    # ----------------------------------------------------------------- resolve

    def resolve(self, name_or_path: str | Path | None) -> Path | None:
        """Resolve ``name_or_path`` to an icon file, or ``None``.

        An existing file path wins; otherwise the value is a bundled name. A value
        that looks like a path (it has a directory component or a ``.png`` suffix)
        is never treated as a bundled name, so a typo in a path fails loudly
        instead of silently loading a bundled icon.
        """
        text = str(name_or_path or "").strip()
        if not text:
            return None
        candidate = Path(text)
        if candidate.is_file():
            return candidate
        if candidate.suffix.lower() == ".png" or "/" in text or "\\" in text:
            return None
        path = self._dir / f"{text}.png"
        return path if path.is_file() else None

    # ---------------------------------------------------------------------- get

    def get_with_status(
        self, name_or_path: str | Path | None = None, size: int = DEFAULT_ICON_SIZE
    ) -> tuple[Image.Image, bool]:
        """Return ``(image, ok)`` for an icon; ``ok`` is False for the generic one."""
        requested = str(name_or_path or "")
        pixels = max(1, int(size))
        key = (requested, pixels)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path = self.resolve(requested)
        if path is None:
            image = self.generic(pixels)
            self._warn(requested, None, "no bundled icon or file with that name")
            ok = False
        else:
            try:
                with Image.open(path) as opened:
                    opened.load()
                    source = opened.convert("RGBA")
                image = (
                    source
                    if source.size == (pixels, pixels)
                    else source.resize((pixels, pixels), Image.Resampling.LANCZOS)
                )
                ok = True
            except Exception as exc:  # an unreadable file must not break a frame
                image = self.generic(pixels)
                self._warn(requested, path, f"unusable icon file: {exc}")
                ok = False
        self._cache[key] = (image, ok)
        return image, ok

    def get(
        self, name_or_path: str | Path | None = None, size: int = DEFAULT_ICON_SIZE
    ) -> Image.Image:
        """Return the icon image for ``name_or_path`` (the generic one on failure)."""
        return self.get_with_status(name_or_path, size)[0]

    def generic(self, size: int = DEFAULT_ICON_SIZE) -> Image.Image:
        """The placeholder drawn when no usable icon file exists."""
        pixels = max(1, int(size))
        cached = self._generic.get(pixels)
        if cached is not None:
            return cached
        image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        inset = max(1, pixels // 8)
        draw.rounded_rectangle(
            [inset, inset, pixels - 1 - inset, pixels - 1 - inset],
            radius=max(1, pixels // 8),
            outline=GENERIC_COLOUR,
            width=max(1, pixels // 16),
        )
        self._generic[pixels] = image
        return image

    # ------------------------------------------------------------------ warning

    def _warn(self, icon: str, path: Path | None, reason: str) -> None:
        event: dict[str, Any] = {
            "event": "icon_unusable",
            "icon": icon,
            "path": str(path) if path is not None else None,
            "reason": reason,
        }
        self.warnings.append(event)
        if self._notify is not None:
            self._notify(event)
