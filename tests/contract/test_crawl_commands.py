"""Contract tests for the crawl verbs (T078, US3).

The shapes come from ``contracts/builtin-plugins.md`` (the ``crawl`` section):
``items set|add|clear``, ``source add|set|remove|list`` and ``preset list``.
``items *`` are ``spoolable`` (an agent must be able to supply headlines while
the runner is down, FR-030); the ``source`` verbs and ``preset list`` are not.
An invalid ``speed_px_s`` is refused by the settings schema — the CLI reports
:class:`~smart_panel.sdk.settings.SettingError` as ``invalid_input`` (exit 2).
"""

from __future__ import annotations

import pytest

from smart_panel.plugins.crawl import CrawlPlugin
from smart_panel.plugins.crawl.presets import list_presets
from smart_panel.sdk.plugin import CommandError
from smart_panel.sdk.settings import SettingError, validate
from smart_panel.sdk.testing import PluginHarness, assert_conforms

CRAWL_VERBS = {
    "items set",
    "items add",
    "items clear",
    "source add",
    "source set",
    "source remove",
    "source list",
    "preset list",
}
SPOOLABLE_VERBS = {"items set", "items add", "items clear"}


def _command(name: str):
    for command in CrawlPlugin.manifest.commands:
        if command.name == name:
            return command
    raise AssertionError(f"no command named {name!r}")


def _arg_names(name: str) -> set[str]:
    return {setting.name for setting in _command(name).args}


# ------------------------------------------------------------------- manifest


def test_the_manifest_declares_a_region_plugin() -> None:
    manifest = CrawlPlugin.manifest
    assert manifest.name == "crawl"
    assert manifest.capabilities == frozenset({"region"})
    assert manifest.animated is True, "a ticker must be rendered every frame"
    assert manifest.source_types == ()


def test_every_documented_verb_is_declared_with_its_arguments() -> None:
    assert {command.name for command in CrawlPlugin.manifest.commands} == CRAWL_VERBS
    assert _arg_names("items set") >= {"items", "source", "stdin"}
    assert _arg_names("items add") >= {"items", "source"}
    assert _arg_names("items clear") >= {"source"}
    assert _arg_names("source add") >= {
        "id",
        "kind",
        "url",
        "preset",
        "template",
        "list_path",
        "interval",
        "label",
        "topic",
        "query",
    }
    assert _arg_names("source set") >= {"id"}
    assert _arg_names("source remove") >= {"id"}
    assert _arg_names("source list") == set()
    assert _arg_names("preset list") == set()


def test_only_the_items_verbs_are_spoolable() -> None:
    for name in CRAWL_VERBS:
        assert _command(name).spoolable is (name in SPOOLABLE_VERBS), name


def test_the_plugin_conforms_to_the_sdk() -> None:
    assert_conforms(CrawlPlugin)


# --------------------------------------------------------------- items verbs


def test_items_set_add_and_clear_report_their_source_and_count() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        assert harness.command("items set", items=["Alpha headline", "Beta headline"]) == {
            "source": "cli",
            "count": 2,
        }
        assert harness.command("items add", items=["Gamma headline"]) == {
            "source": "cli",
            "count": 3,
        }
        assert harness.command("items clear") == {"source": "cli", "count": 0}
    finally:
        harness.stop()


def test_items_set_accepts_a_bare_string_as_one_item() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        assert harness.command("items set", items="Alpha headline")["count"] == 1
    finally:
        harness.stop()


def test_items_are_published_on_the_bus_and_reported_in_state() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        harness.command("items set", items=["Alpha headline", "Beta headline"])
        sample = harness.bus.get("crawl.items.cli")
        assert sample.value == ["Alpha headline", "Beta headline"]
        stated = harness.state()
        assert stated["items"] == 2
        assert stated["sources"]["cli"]["kind"] == "cli"
    finally:
        harness.stop()


def test_items_set_replaces_an_items_key_that_is_not_a_list() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        with pytest.raises(CommandError) as excinfo:
            harness.command("items set", items=17)
        assert excinfo.value.code == "invalid_input"
    finally:
        harness.stop()


# -------------------------------------------------------------- source verbs


def test_source_add_set_remove_and_list_round_trip() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        added = harness.command(
            "source add",
            id="news",
            kind="rss",
            preset="google-news-topic",
            topic="TECHNOLOGY",
        )
        assert added["id"] == "news" and added["kind"] == "rss"
        assert added["url"] == (
            "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY"
            "?hl=en-US&gl=US&ceid=US:en"
        )
        assert added["interval_s"] == 300

        listed = harness.command("source list")
        assert [source["id"] for source in listed["sources"]] == ["news"]

        updated = harness.command("source set", id="news", interval=120)
        assert updated["interval_s"] == 120

        assert harness.command("source remove", id="news") == {
            "id": "news",
            "removed": True,
        }
        assert harness.command("source list")["sources"] == []
    finally:
        harness.stop()


def test_source_add_applies_the_espn_preset() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        added = harness.command("source add", id="nfl", kind="json", preset="espn-nfl")
        assert added["url"] == (
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        )
        assert added["list_path"] == "events"
        assert added["interval_s"] == 60
        assert "{competitions.0.competitors.1.team.abbreviation}" in added["template"]
    finally:
        harness.stop()


def test_an_invalid_source_is_refused_as_invalid_input() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        for args in (
            {"id": "bad", "kind": "bogus", "url": "https://example.com/f.xml"},
            {"id": "bad", "kind": "rss", "url": "file:///etc/passwd"},
            {"id": "bad", "kind": "rss", "url": "https://example.com/f.xml", "interval": 5},
            {"id": "", "kind": "rss", "url": "https://example.com/f.xml"},
            {"kind": "rss", "url": "https://example.com/f.xml"},
        ):
            with pytest.raises(CommandError) as excinfo:
                harness.command("source add", **args)
            assert excinfo.value.code == "invalid_input", args
        with pytest.raises(CommandError) as excinfo:
            harness.command("source set", id="missing", interval=120)
        assert excinfo.value.code == "not_found"
        with pytest.raises(CommandError) as excinfo:
            harness.command("source remove", id="missing")
        assert excinfo.value.code == "not_found"
    finally:
        harness.stop()


def test_preset_list_names_every_shipped_preset() -> None:
    harness = PluginHarness(CrawlPlugin, {}, size=(1728, 80))
    try:
        listed = harness.command("preset list")
        names = [preset["name"] for preset in listed["presets"]]
        assert names == [
            "espn-nfl",
            "espn-nba",
            "espn-mlb",
            "espn-nhl",
            "espn-epl",
            "espn-ncaaf",
            "google-news-search",
            "google-news-topic",
        ]
        assert names == [preset["name"] for preset in list_presets()]
        for preset in listed["presets"]:
            assert set(preset) >= {"name", "kind", "interval_s"}
    finally:
        harness.stop()


# ---------------------------------------------------------------- settings


def test_an_invalid_speed_exits_2_through_the_settings_schema() -> None:
    with pytest.raises(SettingError) as excinfo:
        validate(CrawlPlugin.manifest.settings, {"speed_px_s": 1000})
    assert "speed_px_s" in excinfo.value.path

    assert validate(CrawlPlugin.manifest.settings, {})["speed_px_s"] == 140
    with pytest.raises(SettingError):
        validate(CrawlPlugin.manifest.settings, {"direction": "up"})
    with pytest.raises(SettingError):
        validate(CrawlPlugin.manifest.settings, {"unknown_setting": 1})


def test_a_settings_change_is_adopted_live() -> None:
    live = PluginHarness(CrawlPlugin, {"speed_px_s": 140}, size=(1728, 80))
    try:
        changed = live.set_settings(speed_px_s=200, fg="#00FF00")
        assert changed["speed_px_s"] == 200
        assert live.plugin.settings["speed_px_s"] == 200
        assert live.plugin.settings["fg"] == "#00FF00"
    finally:
        live.stop()
