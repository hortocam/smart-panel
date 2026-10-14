"""Unit tests for crawl rendering (T077, US3).

Covers ``smart_panel/plugins/crawl/render.py`` (clock-derived position, a
seamless loop, left/right direction, item sets swapped at the loop seam, a long
item carried whole, ``round_robin`` versus ``grouped``) and the parts of
``model.py`` the renderer depends on (280-char truncation at a word boundary,
the ``speed_px_s`` range, the placeholder for a crawl that never had items, and
text never below ``min_text_px``).

The clock is a fake, so a "dropped frame" is expressed by rendering twice with a
large time step rather than by sleeping.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PILImage

from smart_panel.core.fonts import FontRegistry
from smart_panel.plugins.crawl.model import (
    DEFAULT_SEPARATOR,
    MAX_ITEM_CHARS,
    CrawlItem,
    order_items,
    truncate_item,
)
from smart_panel.plugins.crawl.render import CrawlRenderer
from smart_panel.sdk.context import FrameContext
from smart_panel.sdk.settings import SettingError, validate
from smart_panel.sdk.testing import FakeClock, PluginHarness

from smart_panel.plugins.crawl import CrawlPlugin  # isort: skip

REGION = (1728, 80)


def _settings(**overrides: object) -> dict:
    return validate(CrawlPlugin.manifest.settings, {"speed_px_s": 140, **overrides})


def _frame(
    clock: FakeClock, *, size: tuple[int, int] = REGION, min_text_px: int = 24
) -> FrameContext:
    return FrameContext(
        size=size,
        now=clock.monotonic(),
        wall=clock.time(),
        frame_index=1,
        fonts=FontRegistry(),
        icons=None,
        bus=None,
        min_text_px=min_text_px,
    )


def _ink(image: PILImage.Image) -> int:
    """The number of pixels that differ from the image's most common colour."""
    colours = image.convert("RGB").getcolors(maxcolors=1 << 20) or []
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(
        count
        for count, colour in image.convert("RGB").getcolors(maxcolors=1 << 20) or []
        if colour != background
    )


# -------------------------------------------------------- clock-derived motion


def test_the_position_is_derived_from_the_clock_not_from_the_frame_count() -> None:
    """A dropped frame must not make the ticker jump (research.md decision 3)."""
    texts = ["Alpha headline", "Beta headline"]
    settings = _settings()

    continuous = CrawlRenderer()
    clock = FakeClock(start=100.0)
    continuous.render(_frame(clock), texts, settings)  # the set is adopted here
    for _ in range(11):
        clock.advance(0.1)
        continuous.render(_frame(clock), texts, settings)
    continuous_last = continuous.render(_frame(clock), texts, settings)

    dropped = CrawlRenderer()
    clock = FakeClock(start=100.0)
    dropped.render(_frame(clock), texts, settings)  # the same adoption instant
    clock.advance(1.1)  # eleven frames' worth of time, one render
    dropped_last = dropped.render(_frame(clock), texts, settings)

    assert list(continuous_last.getdata()) == list(dropped_last.getdata())


def test_the_loop_is_seamless() -> None:
    texts = ["Alpha headline", "Beta headline", "Gamma headline"]
    settings = _settings(speed_px_s=140)
    renderer = CrawlRenderer()
    clock = FakeClock(start=0.0)
    first = renderer.render(_frame(clock), texts, settings)
    width = renderer.strip_width
    assert width > 0
    clock.advance(width / 140)  # exactly one full loop of the strip
    second = renderer.render(_frame(clock), texts, settings)
    assert list(first.getdata()) == list(second.getdata())


def test_direction_left_and_right_are_mirror_images() -> None:
    """A ``right`` crawl is the ``left`` one mirrored within each strip period."""
    texts = ["Alpha headline", "Beta headline"]
    clock = FakeClock(start=0.0)
    left_renderer = CrawlRenderer()
    right_renderer = CrawlRenderer()
    left = left_renderer.render(_frame(clock), texts, _settings(direction="left"))
    right = right_renderer.render(_frame(clock), texts, _settings(direction="right"))
    width = left_renderer.strip_width
    assert width == right_renderer.strip_width
    for x in (0, 40, width - 1, width + 7):
        for y in (0, 20, REGION[1] - 1):
            assert right.getpixel((x, y)) == left.getpixel(((width - 1 - x) % width, y))


def test_speed_moves_the_strip_further_in_the_same_time() -> None:
    texts = ["Alpha headline"]
    slow = CrawlRenderer()
    fast = CrawlRenderer()
    clock = FakeClock(start=0.0)
    slow.render(_frame(clock), texts, _settings(speed_px_s=20))
    fast.render(_frame(clock), texts, _settings(speed_px_s=600))
    clock.advance(1.0)
    assert list(
        slow.render(_frame(clock), texts, _settings(speed_px_s=20)).getdata()
    ) != list(fast.render(_frame(clock), texts, _settings(speed_px_s=600)).getdata())


# ------------------------------------------------------------ item-set swap


def test_items_are_swapped_only_at_the_loop_seam() -> None:
    settings = _settings(speed_px_s=140)
    renderer = CrawlRenderer()
    clock = FakeClock(start=0.0)
    renderer.render(_frame(clock), ["Alpha"], settings)
    assert renderer.strip_texts == ("Alpha",)

    seam = renderer.next_seam(clock.monotonic())
    assert seam > clock.monotonic()

    clock = FakeClock(start=seam - 0.01)
    renderer.render(_frame(clock), ["Beta"], settings)
    assert renderer.strip_texts == ("Alpha",), "the new items must wait for the seam"

    clock = FakeClock(start=seam + 0.01)
    renderer.render(_frame(clock), ["Beta"], settings)
    assert renderer.strip_texts == ("Beta",)


def test_a_long_item_is_carried_whole_rather_than_clipped_to_the_region() -> None:
    long_text = (
        "Supercalifragilistic headline that is far wider than the crawl region "
        "and keeps going for a while after that"
    )
    renderer = CrawlRenderer()
    clock = FakeClock(start=0.0)
    renderer.render(_frame(clock), [long_text], _settings(size=44))
    assert renderer.strip_width > REGION[0], "a long item was clipped instead of scrolled"

    narrow_renderer = CrawlRenderer()
    narrow_renderer.render(_frame(FakeClock(start=0.0)), ["short"], _settings(size=44))
    assert narrow_renderer.strip_width < REGION[0]


def test_a_dropped_item_set_is_still_rendered_with_ink() -> None:
    renderer = CrawlRenderer()
    clock = FakeClock(start=0.0)
    image = renderer.render(_frame(clock), ["Alpha"], _settings())
    assert _ink(image) > 0


# ------------------------------------------------------------------- ordering


def test_round_robin_interleaves_sources_and_grouped_keeps_them_together() -> None:
    by_source = {
        "cli": [CrawlItem("cli one", "cli"), CrawlItem("cli two", "cli")],
        "news": [CrawlItem("news one", "news")],
    }
    assert [item.text for item in order_items(by_source, "grouped")] == [
        "cli one",
        "cli two",
        "news one",
    ]
    assert [item.text for item in order_items(by_source, "round_robin")] == [
        "cli one",
        "news one",
        "cli two",
    ]


def test_order_items_is_stable_for_an_unknown_order() -> None:
    by_source = {"a": [CrawlItem("one", "a")], "b": [CrawlItem("two", "b")]}
    assert [item.text for item in order_items(by_source, "nonsense")] == ["one", "two"]


# -------------------------------------------------------- truncation & bounds


def test_long_text_is_truncated_at_a_word_boundary_with_an_ellipsis() -> None:
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi"
    truncated = truncate_item(text, limit=40)
    assert len(truncated) <= 40
    assert truncated.endswith("…")
    assert text.startswith(truncated[:-1])
    assert not truncated[:-1].endswith(" ")

    words = "word " * 200
    capped = truncate_item(words, limit=MAX_ITEM_CHARS)
    assert len(capped) <= MAX_ITEM_CHARS
    assert capped.endswith("…")
    assert words.startswith(capped[:-1])
    assert not capped[:-1].endswith(" ")

    unbroken = "a" * (MAX_ITEM_CHARS + 50)
    assert len(truncate_item(unbroken)) == MAX_ITEM_CHARS
    assert truncate_item(unbroken).endswith("…")


def test_a_short_text_is_returned_unchanged() -> None:
    assert truncate_item("short text") == "short text"
    assert truncate_item("") == ""


def test_speed_range_and_default() -> None:
    assert _settings()["speed_px_s"] == 140
    with pytest.raises(SettingError):
        _settings(speed_px_s=601)
    with pytest.raises(SettingError):
        _settings(speed_px_s=19)


def test_separator_limit_and_default() -> None:
    assert _settings()["separator"] == DEFAULT_SEPARATOR
    with pytest.raises(SettingError):
        _settings(separator="too long!!")


# ------------------------------------------- placeholder and the text floor


def test_the_placeholder_is_shown_when_there_were_never_any_items() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=REGION)
    try:
        first = harness.render(0.0)
        assert first is not None and first.size == REGION
        assert _ink(first) > 0, "the placeholder text must be drawn"
        second = harness.render(10.0)
        assert list(first.getdata()) == list(second.getdata()), "the placeholder must not scroll"
    finally:
        harness.stop()


def test_text_is_never_below_min_text_px() -> None:
    below = PluginHarness(CrawlPlugin, {"size": 12}, size=REGION)
    at_floor = PluginHarness(CrawlPlugin, {"size": 24}, size=REGION)
    try:
        small = below.render(0.0)
        floor = at_floor.render(0.0)
        assert small is not None and floor is not None
        assert list(small.getdata()) == list(floor.getdata()), (
            "a size below min_text_px must be clamped up, not drawn smaller"
        )
    finally:
        below.stop()
        at_floor.stop()


def test_an_empty_item_list_after_items_were_set_paints_the_background() -> None:
    """A crawl whose items were cleared keeps its own background, not the canvas."""
    harness = PluginHarness(CrawlPlugin, {"bg": "#B00020"}, size=REGION)
    try:
        harness.command("items set", items=["Alpha"])
        with_items = harness.render(0.0)
        assert with_items is not None and _ink(with_items) > 0
        harness.command("items clear")
        cleared = harness.render(0.5)
        assert cleared is not None and cleared.size == REGION
        assert cleared.convert("RGB").getpixel((10, 10)) == (176, 0, 32)
    finally:
        harness.stop()


def test_the_fixtures_directory_is_where_the_sources_tests_expect_it() -> None:
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    assert (fixtures / "google_news_sample.xml").is_file()
    assert (fixtures / "espn_nfl_scoreboard.json").is_file()
    assert (fixtures / "atom_sample.xml").is_file()
