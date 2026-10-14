"""Bundled icons: resolution by name or PNG path, generic fallback (T105, US5).

The sidebar's icons ship under :data:`ICON_DIR` as 64 px PNGs derived from
Tabler Icons (MIT — see ``THIRD_PARTY_NOTICES.md``). A plugin asks for an icon by
its bundled name (``"mail"``) or by the path of any PNG file (FR-038), and always
gets a square image back:

* a name that is not bundled, and a path that does not exist, resolve to ``None``
  from :meth:`IconRegistry.resolve`;
* a name that resolves but whose file cannot be decoded falls back to the
  *generic* icon, and the failure is recorded in :attr:`IconRegistry.warnings`
  (and sent to the optional ``notify`` callback, once per distinct failure) so
  ``status`` lists it instead of a broken icon appearing silently.

A warning is not permanent: the cache remembers the source file's identity
(mtime and size) for a *successful* resolution, while a *failed* one is re-read on
every lookup, so an icon repaired at the same path — even at the same byte length
inside one timestamp tick — resolves again, and when it does its warning is dropped
from :attr:`IconRegistry.warnings`: ``status`` lists the icons that are unusable
*now*, not the ones that once were.

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
    """Resolve icons and cache them by (name, size) and source identity.

    A cache entry also remembers the resolved file's identity (mtime and size, or
    ``None`` while it does not exist). A *successful* resolution is trusted while
    that identity is unchanged; a *failed* one is re-read on every lookup, so an
    icon file repaired or created at the same path resolves again instead of
    serving the generic fallback until a restart.

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
        self._cache: dict[tuple[str, int], tuple[Image.Image, bool, tuple[int, int] | None]] = {}
        self._generic: dict[int, Image.Image] = {}
        self._reported: dict[str, tuple[dict[str, Any], tuple[int, int] | None]] = {}

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
        """Return ``(image, ok)`` for an icon; ``ok`` is False for the generic one.

        The cache is keyed by ``(name, size)``. A *successful* resolution is served
        from the cache while the resolved file's stamp (mtime and size) is unchanged;
        a *failed* resolution does not trust that stamp and re-reads the file on every
        call, so an icon repaired at the same path — including a same-byte-length
        repair inside one timestamp tick — resolves again rather than serving the
        fallback the first failure cached. An icon that resolves again drops its entry
        from :attr:`warnings`, and a failure that simply repeats is not re-notified
        while nothing about it has changed (T105).
        """
        requested = str(name_or_path or "")
        pixels = max(1, int(size))
        key = (requested, pixels)
        path = self.resolve(requested)
        stamp = self._stamp(path)
        cached = self._cache.get(key)
        if cached is not None and cached[1] and cached[2] == stamp:
            # Only a cached *success* may take the identity fast path. The stamp
            # ``(mtime_ns, size)`` is not a complete identity — a file replaced at the
            # same byte length inside one coarse-clock tick keeps it — so a cached
            # failure is never trusted: the file is re-read on every lookup until it
            # resolves. A broken icon is the rare, transient case; a working one is
            # the hot path, and it is unchanged.
            return cached[0], cached[1]
        if path is None:
            image = self.generic(pixels)
            ok = False
            self._warn(requested, None, "no bundled icon or file with that name", stamp)
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
                ok = False
                self._warn(requested, path, f"unusable icon file: {exc}", stamp)
        if ok:
            self._clear(requested)  # it resolves now; an earlier warning is stale
        self._cache[key] = (image, ok, stamp)
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

    @staticmethod
    def _stamp(path: Path | None) -> tuple[int, int] | None:
        """The resolved file's identity (mtime and size), or ``None`` when it is absent.

        The stamp is a fast path only, and only for a *success* (see
        :meth:`get_with_status`). It is not a complete identity: on a coarse
        filesystem clock two writes microseconds apart can share one ``st_mtime_ns``,
        so a same-byte-length change at the same path can leave the stamp unchanged.
        A cached *failure* therefore never relies on it — the file is re-read — which
        is what makes a repaired icon recoverable at any timestamp granularity.
        """
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:  # the file vanished between resolve() and here
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _warn(
        self, icon: str, path: Path | None, reason: str, stamp: tuple[int, int] | None
    ) -> None:
        event: dict[str, Any] = {
            "event": "icon_unusable",
            "icon": icon,
            "path": str(path) if path is not None else None,
            "reason": reason,
        }
        # One warning per icon: a repeated failure replaces its own earlier entry
        # rather than accumulating duplicates, so ``status`` lists each unusable
        # icon once and the list cannot grow without bound.
        #
        # A still-failing file is re-read on every lookup (see ``get_with_status``),
        # so the *same* condition — the same icon, path and reason, with the file
        # unchanged since the last report — is not notified again: ``status`` already
        # lists it and the log would otherwise gain one line per frame. A genuinely
        # different condition (another file state, path or reason) is a new event and
        # still notifies.
        repeated = self._reported.get(icon) == (event, stamp)
        self._clear(icon)
        self.warnings.append(event)
        self._reported[icon] = (event, stamp)
        if self._notify is not None and not repeated:
            self._notify(event)

    def _clear(self, icon: str) -> None:
        """Drop any warning for ``icon`` (it resolves again, or is re-reported)."""
        self.warnings[:] = [entry for entry in self.warnings if entry.get("icon") != icon]
        self._reported.pop(icon, None)
