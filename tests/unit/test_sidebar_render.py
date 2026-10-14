"""Unit tests for the sidebar: icons, the entry model and rendering (T102, US5).

Covers ``smart_panel/core/icons.py`` (T105), ``smart_panel/plugins/sidebar/model.py``
(T106) and ``smart_panel/plugins/sidebar/render.py`` (T107), against the US5
requirements in ``contracts/builtin-plugins.md`` and ``data-model.md`` (Sidebar
Entry, FR-037 to FR-041):

* bundled icons resolve by name or PNG path, and an unusable image falls back to
  the generic icon with a warning ``status`` lists;
* entries are at least 72 px tall and at most 6 including the built-in alert
  entry (a 7th is rejected naming the maximum);
* ``hide_when_zero`` defaults true, ``max_display`` defaults 999 (larger counts
  render ``999+``), a stale count renders ``?``;
* label and chip text are never below ``min_text_px`` (both default 24 and are
  clamped up if configured lower);
* a changed count gets a brief highlight;
* ``label`` is at most 12 characters;
* the alert entry takes the colour of the highest missed severity and hides at
  zero.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image as PILImage

from smart_panel.core.fonts import FontRegistry
from smart_panel.core.icons import (
    GENERIC_NAME,
    IconRegistry,
    bundled_icons,
)
from smart_panel.plugins.sidebar import SidebarPlugin
from smart_panel.plugins.sidebar.model import (
    ALERT_ENTRY_ID,
    BUNDLED_ICONS,
    LABEL_MAX_CHARS,
    MAX_ENTRIES,
    MIN_ENTRY_HEIGHT_PX,
    STALE_TEXT,
    SidebarEntry,
    SidebarEntrySet,
    SidebarError,
    alert_entry,
    entry_height,
)
from smart_panel.plugins.sidebar.render import (
    HIGHLIGHT_COLOR,
    HIGHLIGHT_S,
    SidebarReading,
    SidebarRenderer,
    chip_text,
    icon_box,
)
from smart_panel.sdk.context import FrameContext, ReadOnlyBus
from smart_panel.sdk.plugin import Image as RenderImage
from smart_panel.sdk.plugin import Unchanged
from smart_panel.sdk.settings import validate
from smart_panel.sdk.testing import PluginHarness

REGION = (192, 462)
CHIP_BG = (214, 40, 57)  # #D62839
CRITICAL = (214, 40, 57)  # #D62839
INFO = (46, 125, 215)  # #2E7DD7
WARNING = (230, 167, 0)  # #E6A700
BG = (11, 14, 17)  # #0B0E11


def _frame(
    *,
    size: tuple[int, int] = REGION,
    min_text_px: int = 24,
    now: float = 0.0,
    icons: object = None,
) -> FrameContext:
    return FrameContext(
        size=size,
        now=now,
        wall=0.0,
        frame_index=1,
        fonts=FontRegistry(),
        icons=icons,
        bus=None,
        min_text_px=min_text_px,
    )


def _settings(**overrides: object) -> dict:
    return validate(SidebarPlugin.manifest.settings, overrides)


def _entry(index: int = 0, **overrides: object) -> SidebarEntry:
    fields: dict = {
        "id": f"e{index}",
        "icon": "mail",
        "label": f"E{index}",
        "topic": f"count.e{index}",
    }
    fields.update(overrides)
    return SidebarEntry.from_dict(fields)


def _reading(
    count: int | None = 5, *, stale: bool = False, severity: str | None = None
) -> SidebarReading:
    return SidebarReading(count=count, stale=stale, severity=severity)


def _rendered(
    renderer: SidebarRenderer,
    frame: FrameContext,
    entries: list,
    readings: list,
    settings: dict,
) -> PILImage.Image:
    """Render once and unwrap the ``Image`` result (``Unchanged`` is a test error)."""
    result = renderer.render(frame, entries, readings, settings)
    assert isinstance(result, RenderImage), f"expected an image, got {result!r}"
    return result.image


def _count(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    """How many pixels of ``image`` are exactly ``colour`` (RGB)."""
    counts = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    return sum(count for count, rgb in counts if rgb == colour)


def _ink(image: PILImage.Image) -> int:
    """Pixels that differ from the image's most common colour."""
    colours = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


# ------------------------------------------------------------- icons (T105)


def test_every_bundled_icon_is_a_64px_png() -> None:
    registry = IconRegistry()
    assert set(bundled_icons()) == set(BUNDLED_ICONS)
    for name in BUNDLED_ICONS:
        path = registry.resolve(name)
        assert path is not None and path.is_file(), f"{name} is not bundled"
        with PILImage.open(path) as image:
            assert image.size == (64, 64), f"{name} is {image.size}, not 64x64"


def test_an_icon_resolves_by_bundled_name_or_by_png_path(tmp_path: Path) -> None:
    registry = IconRegistry()
    assert registry.resolve("mail") is not None
    custom = tmp_path / "mine.png"
    PILImage.new("RGBA", (64, 64), (255, 255, 255, 255)).save(custom)
    assert registry.resolve(str(custom)) == custom
    assert registry.resolve("definitely-not-an-icon") is None
    assert registry.resolve(str(tmp_path / "missing.png")) is None


def test_an_unusable_icon_falls_back_to_the_generic_icon_with_a_warning(tmp_path: Path) -> None:
    registry = IconRegistry()
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"this is not a PNG")

    image = registry.get(str(broken), 64)
    assert image.size == (64, 64)
    assert image.tobytes() == registry.generic(64).tobytes(), (
        "an unusable icon file must fall back to the generic icon"
    )
    assert registry.warnings, "the fallback must raise a warning that status can list"
    assert "broken.png" in str(registry.warnings[-1])

    unknown = registry.get("no-such-icon", 64)
    assert unknown.tobytes() == registry.generic(64).tobytes()
    assert registry.get(GENERIC_NAME, 64).size == (64, 64)


def test_a_file_fixed_at_the_same_path_stops_warning_and_refreshes_the_cache(
    tmp_path: Path,
) -> None:
    """A PNG replaced at the same path must resolve again, not serve a stale fallback.

    ``IconRegistry`` caches by ``(name, size)``, so a file that was unusable and is
    then rewritten at the same path would otherwise keep returning the generic
    fallback (and keep listing its warning) until the process restarts.
    """
    registry = IconRegistry()
    icon = tmp_path / "mail.png"
    icon.write_bytes(b"this is not a PNG")
    assert registry.get_with_status(str(icon), 64)[1] is False
    assert [warning["icon"] for warning in registry.warnings] == [str(icon)]

    PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(icon)

    image, ok = registry.get_with_status(str(icon), 64)
    assert ok is True, "an icon that resolves again must not keep reporting the fallback"
    assert image.tobytes() != registry.generic(64).tobytes(), (
        "a replaced file must not keep serving the cached fallback image"
    )
    assert registry.warnings == [], "a warning must not outlive the unusable icon"


def test_an_icon_that_appears_later_resolves_and_stops_warning(tmp_path: Path) -> None:
    """A path that did not exist yet is re-resolved, not pinned to its first failure."""
    registry = IconRegistry()
    later = tmp_path / "later.png"
    assert registry.get_with_status(str(later), 64)[1] is False
    assert [warning["icon"] for warning in registry.warnings] == [str(later)]

    PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(later)

    image, ok = registry.get_with_status(str(later), 64)
    assert ok is True
    assert image.tobytes() != registry.generic(64).tobytes()
    assert registry.warnings == []


def test_a_same_length_repair_at_the_same_stamp_is_still_re_read(tmp_path: Path) -> None:
    """``(mtime_ns, size)`` is not a complete identity, so a failed entry is re-read.

    Two writes microseconds apart can share one ``st_mtime_ns`` on a coarse clock, so
    a file repaired at the same byte length inside the same tick would otherwise never
    be re-read and its fallback would stick forever. The stamp is pinned here so the
    test does not depend on the host clock's granularity.
    """
    registry = IconRegistry()
    icon = tmp_path / "mail.png"
    PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(icon)
    good = icon.read_bytes()
    when = (1_700_000_000_000_000_000, 1_700_000_000_000_000_000)  # fixed (atime, mtime)

    icon.write_bytes(b"\x00" * len(good))  # same length, no longer a PNG
    os.utime(icon, ns=when)
    assert registry.get_with_status(str(icon), 64)[1] is False
    assert [warning["icon"] for warning in registry.warnings] == [str(icon)]

    icon.write_bytes(good)  # repaired: same path, same byte length, same stamp
    os.utime(icon, ns=when)
    image, ok = registry.get_with_status(str(icon), 64)
    assert ok is True, "a same-length repair must not be masked by a cached failure"
    assert image.tobytes() != registry.generic(64).tobytes()
    assert registry.warnings == [], "the stale warning must be dropped once it resolves"


def test_an_identical_failure_is_not_re_notified_but_a_new_one_is(tmp_path: Path) -> None:
    """A still-failing file is re-read every lookup without spamming the log.

    ``status`` already lists the icon, so an identical ``icon_unusable`` event is
    suppressed on a repeat re-read; a genuinely different condition still notifies.
    """
    events: list[dict] = []
    registry = IconRegistry(notify=events.append)
    icon = tmp_path / "mail.png"
    icon.write_bytes(b"nope")

    assert registry.get_with_status(str(icon), 64)[1] is False
    assert registry.get_with_status(str(icon), 64)[1] is False
    assert registry.get_with_status(str(icon), 64)[1] is False
    assert len(events) == 1, f"an identical repeat must not re-notify: {events}"
    assert len(registry.warnings) == 1, "one entry per icon, not one per lookup"

    icon.unlink()  # now a different condition: the file is gone
    assert registry.get_with_status(str(icon), 64)[1] is False
    assert len(events) == 2, f"a different failure must still notify: {events}"


def test_the_sidebar_lists_its_icon_fallbacks_in_status(tmp_path: Path) -> None:
    harness = PluginHarness(
        SidebarPlugin,
        {"entries": [_entry(0, icon="nowhere").to_dict()]},
        size=REGION,
    )
    try:
        broken = tmp_path / "broken.png"
        broken.write_bytes(b"nope")
        frame = FrameContext(
            size=REGION,
            now=0.0,
            wall=0.0,
            frame_index=1,
            fonts=FontRegistry(),
            icons=IconRegistry(),
            bus=ReadOnlyBus(harness.bus),
            min_text_px=24,
        )
        harness.plugin.render(frame)
        warnings = harness.state()["warnings"]
        assert any("nowhere" in str(entry) for entry in warnings), warnings
    finally:
        harness.stop()


def test_a_recovered_icon_drops_its_fallback_from_status(tmp_path: Path) -> None:
    """``status`` must stop listing a fallback once the icon resolves again.

    The sidebar's own region cache is refreshed on a settings change (``forget``),
    which is when the icons are re-resolved: an icon fixed by then must leave no
    stale ``warnings`` entry behind. A cached region also re-checks its fallbacks on
    its own (see the renderer below), so a settings change is one way to force it,
    not the only one.
    """
    icon = tmp_path / "mail.png"
    icon.write_bytes(b"nope")
    harness = PluginHarness(
        SidebarPlugin,
        {"entries": [_entry(0, icon=str(icon)).to_dict()]},
        size=REGION,
    )
    try:
        frame = FrameContext(
            size=REGION,
            now=0.0,
            wall=0.0,
            frame_index=1,
            fonts=FontRegistry(),
            icons=IconRegistry(),
            bus=ReadOnlyBus(harness.bus),
            min_text_px=24,
        )
        harness.plugin.render(frame)
        assert harness.state()["warnings"], "the broken icon must be listed while it is broken"

        PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(icon)
        harness.set_settings(entries=[_entry(0, icon=str(icon)).to_dict()])
        harness.plugin.render(frame)
        assert harness.state()["warnings"] == [], (
            "a recovered icon must not leave a stale fallback in status"
        )
    finally:
        harness.stop()


def test_a_cached_region_re_checks_a_fallback_so_a_repair_shows_on_the_next_frame(
    tmp_path: Path,
) -> None:
    """A repaired icon drops from ``status`` on the next frame, not the next rebuild.

    The region image is cached until something *visible* changes, so ``_draw`` — and
    with it the icon resolution — is skipped while the sidebar is static. The renderer
    therefore re-resolves the icons it currently records as unusable before it serves
    a cached frame: the repair is noticed, the region is rebuilt so the real icon is
    drawn, and ``status`` stops listing the old warning without waiting for a settings
    change or a restart.
    """
    icon = tmp_path / "mail.png"
    icon.write_bytes(b"nope")
    harness = PluginHarness(
        SidebarPlugin,
        {"entries": [_entry(0, icon=str(icon)).to_dict()]},
        size=REGION,
    )
    try:
        frame = FrameContext(
            size=REGION,
            now=0.0,
            wall=0.0,
            frame_index=1,
            fonts=FontRegistry(),
            icons=IconRegistry(),
            bus=ReadOnlyBus(harness.bus),
            min_text_px=24,
        )
        harness.plugin.render(frame)
        assert harness.state()["warnings"], "the broken icon must be listed while it is broken"

        PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(icon)

        # Same frame, same settings: nothing *visible* changed, so the region key is
        # unchanged and the cache would answer ``Unchanged`` without the re-check.
        result = harness.plugin.render(frame)
        assert isinstance(result, RenderImage), (
            "a repaired icon must rebuild the region so the real icon is drawn"
        )
        assert harness.state()["warnings"] == [], (
            "a repaired icon must drop out of status on the next frame, not the next rebuild"
        )

        # And the rebuilt region is cached again: a settled sidebar costs no new build.
        assert isinstance(harness.plugin.render(frame), Unchanged)
    finally:
        harness.stop()


def test_documented_limitation_a_cached_success_trusts_its_stamp(tmp_path: Path) -> None:
    """A cached *success* trusts the ``(mtime_ns, size)`` stamp — including its blind spot.

    The stamp is not a complete identity: measured on this filesystem, two writes
    microseconds apart can share one ``st_mtime_ns``. A same-byte-length corruption
    whose stamp is unchanged therefore keeps serving the previously decoded image (the
    stamp is pinned explicitly here so the test does not depend on the host clock's
    granularity). There is no bounded-cadence re-verification of a cached success, so
    this is documented, measured behavior rather than a guarantee; as soon as the stamp
    changes the failure *is* re-read and reported.
    """
    registry = IconRegistry()
    icon = tmp_path / "mail.png"
    PILImage.new("RGBA", (64, 64), (10, 20, 30, 255)).save(icon)
    good = icon.read_bytes()
    decoded = registry.get_with_status(str(icon), 64)[0].tobytes()
    stamp = icon.stat()
    pinned = (stamp.st_atime_ns, stamp.st_mtime_ns)  # the successful read's own stamp

    icon.write_bytes(b"\x00" * len(good))  # same byte length, no longer a PNG
    os.utime(icon, ns=pinned)
    stale_image, ok = registry.get_with_status(str(icon), 64)
    assert ok is True and stale_image.tobytes() == decoded, (
        "documented limitation: an unchanged stamp serves the cached success"
    )

    os.utime(icon, ns=(pinned[0] + 1_000_000, pinned[1] + 1_000_000))  # the stamp changes
    assert registry.get_with_status(str(icon), 64)[1] is False, (
        "a changed stamp must be re-read, so a corruption is reported once the stamp moves"
    )


# ------------------------------------------------------- the entry model (T106)


def test_six_entries_fit_and_a_seventh_is_rejected_naming_the_maximum() -> None:
    assert MAX_ENTRIES == 6
    assert entry_height(MAX_ENTRIES, REGION[1]) >= MIN_ENTRY_HEIGHT_PX
    assert entry_height(1, REGION[1]) >= MIN_ENTRY_HEIGHT_PX

    entries = SidebarEntrySet()
    for index in range(MAX_ENTRIES - 1):
        entries.add(_entry(index))
    assert len(entries) == MAX_ENTRIES - 1
    with pytest.raises(SidebarError) as excinfo:
        entries.add(_entry(99))
    message = str(excinfo.value)
    assert "6" in message, message
    assert "maximum" in message.lower(), message
    assert "alert" in message.lower(), message


def test_a_settings_list_of_too_many_entries_is_refused() -> None:
    with pytest.raises(SidebarError) as excinfo:
        SidebarEntrySet.from_settings([_entry(i).to_dict() for i in range(MAX_ENTRIES)])
    assert "6" in str(excinfo.value)


def test_the_entry_defaults_are_the_documented_ones() -> None:
    entry = SidebarEntry.from_dict({"id": "mail", "icon": "mail", "topic": "count.mail"})
    assert entry.hide_when_zero is True
    assert entry.max_display == 999
    assert entry.stale_after_s is None
    assert entry.label == "mail"


def test_a_label_longer_than_twelve_characters_is_refused() -> None:
    with pytest.raises(SidebarError) as excinfo:
        SidebarEntry.from_dict(
            {
                "id": "mail",
                "icon": "mail",
                "topic": "count.mail",
                "label": "x" * (LABEL_MAX_CHARS + 1),
            }
        )
    assert str(LABEL_MAX_CHARS) in str(excinfo.value)
    assert len(_entry(0, label="x" * LABEL_MAX_CHARS).label) == LABEL_MAX_CHARS


def test_a_count_above_max_display_is_abbreviated_and_a_zero_hides() -> None:
    entry = _entry(0, max_display=999)
    assert entry.display_count(1204) == "999+"
    assert entry.display_count(999) == "999"
    assert entry.display_count(0) == "0"

    assert chip_text(entry, _reading(1204)) == "999+"
    assert chip_text(entry, _reading(0)) is None, "hide_when_zero defaults true"
    assert chip_text(_entry(0, hide_when_zero=False), _reading(0)) == "0"
    assert chip_text(entry, _reading(42, stale=True)) == STALE_TEXT
    assert chip_text(entry, _reading(None)) == STALE_TEXT


# -------------------------------------------------------------- rendering (T107)


def test_the_background_is_painted_and_rows_carry_content() -> None:
    entries = [_entry(index) for index in range(3)]
    readings = [_reading(3 + index) for index in range(len(entries))]
    image = _rendered(SidebarRenderer(), _frame(), entries, readings, _settings())
    assert image.size == REGION
    assert image.convert("RGB").getpixel((1, 1)) == BG
    assert _ink(image) > 0


def test_no_entries_paint_the_background_only() -> None:
    image = _rendered(SidebarRenderer(), _frame(), [], [], _settings(bg="#0B0E11"))
    assert image.size == REGION
    assert image.convert("RGB").getpixel((5, 5)) == BG
    assert _ink(image) == 0


def test_a_chip_is_drawn_in_the_configured_chip_colour() -> None:
    image = _rendered(
        SidebarRenderer(), _frame(), [_entry(0)], [_reading(7)], _settings()
    )
    assert _count(image, CHIP_BG) > 0
    hidden = _rendered(SidebarRenderer(), _frame(), [_entry(0)], [_reading(0)], _settings())
    assert _count(hidden, CHIP_BG) == 0, "a zero count is hidden by default"


def test_the_label_and_chip_sizes_are_clamped_up_to_min_text_px() -> None:
    entry = _entry(0)
    floor = 32

    def render(**settings: object) -> PILImage.Image:
        return _rendered(
            SidebarRenderer(),
            _frame(min_text_px=floor),
            [entry],
            [_reading(7)],
            _settings(**settings),
        )

    assert _settings()["label_size"] == 24
    assert _settings()["chip_size"] == 24
    assert render(label_size=12, chip_size=32).tobytes() == render(
        label_size=floor, chip_size=32
    ).tobytes(), "a label_size below min_text_px must be clamped up"
    assert render(label_size=24, chip_size=12).tobytes() == render(
        label_size=24, chip_size=floor
    ).tobytes(), "a chip_size below min_text_px must be clamped up"


def test_a_changed_count_gets_a_brief_highlight() -> None:
    entry = _entry(0)
    renderer = SidebarRenderer()
    settings = _settings()

    first = _rendered(renderer, _frame(now=0.0), [entry], [_reading(5)], settings)
    assert _count(first, HIGHLIGHT_COLOR[:3]) == 0, "the first count is not a change"

    changed = _rendered(renderer, _frame(now=0.1), [entry], [_reading(6)], settings)
    assert _count(changed, HIGHLIGHT_COLOR[:3]) > 0, "a changed count must be highlighted"

    settled = _rendered(
        renderer, _frame(now=0.1 + HIGHLIGHT_S + 0.1), [entry], [_reading(6)], settings
    )
    assert _count(settled, HIGHLIGHT_COLOR[:3]) == 0, "the highlight must be brief"


def test_the_alert_entry_takes_the_highest_severity_colour_and_hides_at_zero() -> None:
    entry = alert_entry()
    assert entry.id == ALERT_ENTRY_ID
    assert entry.icon == "alert"
    assert entry.hide_when_zero is True

    for severity, colour in (("info", INFO), ("warning", WARNING), ("critical", CRITICAL)):
        image = _rendered(
            SidebarRenderer(), _frame(), [entry], [_reading(3, severity=severity)], _settings()
        )
        assert _count(image, colour) > 0, f"the {severity} colour must be drawn"

    zero = _rendered(SidebarRenderer(), _frame(), [entry], [_reading(0)], _settings())
    assert _count(zero, CRITICAL) == 0
    assert _count(zero, INFO) == 0
    assert chip_text(entry, _reading(0)) is None


def test_the_plugin_draws_the_built_in_alert_entry() -> None:
    """FR-041: the alert entry is implicit and always occupies a row."""
    harness = PluginHarness(SidebarPlugin, {"entries": []}, size=REGION)
    try:

        def frame() -> FrameContext:
            return FrameContext(
                size=REGION,
                now=0.0,
                wall=0.0,
                frame_index=1,
                fonts=FontRegistry(),
                icons=IconRegistry(),
                bus=ReadOnlyBus(harness.bus),
                min_text_px=24,
            )

        idle = harness.plugin.render(frame())
        assert isinstance(idle, RenderImage)
        image = idle.image
        assert image.size == REGION
        left, top, pixels = icon_box(0, 1, REGION)
        assert _ink(image.crop((left, top, left + pixels, top + pixels))) > 0, (
            "the implicit alert entry must be drawn"
        )

        harness.command("entry add", id="mail", icon="mail", topic="count.mail")
        added = harness.plugin.render(frame())
        assert isinstance(added, RenderImage)
        left, top, pixels = icon_box(1, 2, REGION)
        assert _ink(added.image.crop((left, top, left + pixels, top + pixels))) > 0, (
            "the alert entry's icon is drawn in the second row"
        )
    finally:
        harness.stop()


def test_an_unusable_icon_renders_the_generic_icon_pixels(tmp_path: Path) -> None:
    registry = IconRegistry()
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    entry = _entry(0, icon=str(broken))

    renderer = SidebarRenderer()
    image = _rendered(
        renderer, _frame(size=(192, 96), icons=registry), [entry], [_reading(7)], _settings()
    )

    left, top, size = icon_box(0, 1, (192, 96))
    expected = PILImage.new("RGBA", (192, 96), (*BG, 255))
    expected.paste(registry.generic(size), (left, top), registry.generic(size))
    assert image.crop((left, top, left + size, top + size)).tobytes() == expected.crop(
        (left, top, left + size, top + size)
    ).tobytes()
    assert renderer.fallbacks, "the renderer must record the fallback"
