"""Contract tests for the sidebar verbs and settings (T103, US5).

The shapes come from ``contracts/builtin-plugins.md`` (the ``sidebar`` section)
and ``data-model.md`` (Sidebar Entry): the verbs ``entry add|set|remove|list``
and ``count set ID N``; ``count set`` is the only spoolable verb; an unknown icon
name or a non-integer count is refused as ``invalid_input`` (exit 2); the
settings ``entries``, ``bg``, ``chip_fg``, ``chip_bg``, ``font``, ``label_size``
and ``chip_size`` carry the documented defaults; the built-in alert entry is
present and bound to ``alerts.missed``; and ``assert_conforms`` passes.
"""

from __future__ import annotations

import pytest

from smart_panel.plugins.sidebar import SidebarPlugin
from smart_panel.plugins.sidebar.model import (
    ALERT_TOPIC,
    MAX_ENTRIES,
    STALE_TEXT,
    SidebarError,
)
from smart_panel.sdk.plugin import CommandError, SettingsRejected
from smart_panel.sdk.settings import SettingError, validate
from smart_panel.sdk.testing import PluginHarness, assert_conforms

SIDEBAR_VERBS = {"entry add", "entry set", "entry remove", "entry list", "count set"}
SPOOLABLE_VERBS = {"count set"}


def _command(name: str):
    for command in SidebarPlugin.manifest.commands:
        if command.name == name:
            return command
    raise AssertionError(f"no command named {name!r}")


def _arg_names(name: str) -> set[str]:
    return {setting.name for setting in _command(name).args}


def _harness(**settings: object) -> PluginHarness:
    return PluginHarness(SidebarPlugin, settings, size=(192, 462))


def _add(harness: PluginHarness, identifier: str, **overrides: object) -> dict:
    fields: dict = {
        "id": identifier,
        "icon": "mail",
        "label": identifier.title(),
        "topic": f"count.{identifier}",
    }
    fields.update(overrides)
    return harness.command("entry add", **fields)


# ------------------------------------------------------------------- manifest


def test_the_manifest_declares_a_region_plugin_with_the_six_entry_limit() -> None:
    manifest = SidebarPlugin.manifest
    assert manifest.name == "sidebar"
    assert manifest.capabilities == frozenset({"region"})
    assert manifest.source_types == ()
    assert MAX_ENTRIES == 6


def test_every_documented_verb_is_declared_with_its_arguments() -> None:
    assert {command.name for command in SidebarPlugin.manifest.commands} == SIDEBAR_VERBS
    assert _arg_names("entry add") >= {
        "id",
        "icon",
        "label",
        "topic",
        "hide_when_zero",
        "show_zero",
        "max_display",
    }
    assert _arg_names("entry set") >= {"id", "icon", "label", "topic", "max_display"}
    assert _arg_names("entry remove") >= {"id"}
    assert _arg_names("entry list") == set()
    assert _arg_names("count set") >= {"id", "value"}


def test_only_count_set_is_spoolable() -> None:
    for name in SIDEBAR_VERBS:
        assert _command(name).spoolable is (name in SPOOLABLE_VERBS), name


def test_the_plugin_conforms_to_the_sdk() -> None:
    assert_conforms(SidebarPlugin)


def test_the_declared_settings_match_the_contract() -> None:
    settings = validate(SidebarPlugin.manifest.settings, {})
    assert settings["entries"] == []
    assert settings["bg"] == "#0B0E11"
    assert settings["chip_fg"] == "#FFFFFF"
    assert settings["chip_bg"] == "#D62839"
    assert settings["font"] == "DejaVuSans"
    assert settings["label_size"] == 24
    assert settings["chip_size"] == 24

    with pytest.raises(SettingError):
        validate(SidebarPlugin.manifest.settings, {"bg": "not-a-colour"})
    with pytest.raises(SettingError):
        validate(SidebarPlugin.manifest.settings, {"chip_size": "big"})
    with pytest.raises(SettingError):
        validate(SidebarPlugin.manifest.settings, {"unknown": 1})


# ---------------------------------------------------------------- entry verbs


def test_entry_add_set_remove_and_list_round_trip() -> None:
    harness = _harness()
    try:
        added = _add(harness, "mail", icon="mail", label="Mail", topic="count.mail")
        assert added["id"] == "mail"
        assert added["icon"] == "mail"
        assert added["label"] == "Mail"
        assert added["topic"] == "count.mail"
        assert added["hide_when_zero"] is True
        assert added["max_display"] == 999
        assert added["count"] == 1

        listed = harness.command("entry list")
        assert listed["count"] == 1
        assert listed["max"] == MAX_ENTRIES
        assert [entry["id"] for entry in listed["entries"]] == ["mail"]

        changed = harness.command(
            "entry set", id="mail", label="Inbox", max_display=49, show_zero=True
        )
        assert changed["label"] == "Inbox"
        assert changed["max_display"] == 49
        assert changed["hide_when_zero"] is False, "--show-zero turns hide_when_zero off"

        removed = harness.command("entry remove", id="mail")
        assert removed == {"id": "mail", "removed": True, "count": 0}
        assert harness.command("entry list")["entries"] == []
    finally:
        harness.stop()


def test_a_sixth_configured_entry_is_rejected_naming_the_maximum() -> None:
    harness = _harness()
    try:
        for index in range(MAX_ENTRIES - 1):
            _add(harness, f"e{index}")
        assert harness.command("entry list")["count"] == MAX_ENTRIES - 1

        with pytest.raises(CommandError) as excinfo:
            _add(harness, "e9")
        assert excinfo.value.code == "invalid_input"
        message = str(excinfo.value.message)
        assert "6" in message, message
        assert "maximum" in message.lower(), message
        assert "alert" in message.lower(), message
    finally:
        harness.stop()


def test_an_unknown_icon_name_is_refused_but_a_png_path_is_accepted(tmp_path) -> None:
    from PIL import Image as PILImage

    harness = _harness()
    try:
        with pytest.raises(CommandError) as excinfo:
            _add(harness, "mail", icon="brand-logo")
        assert excinfo.value.code == "invalid_input"
        assert "icon" in str(excinfo.value.message).lower()

        custom = tmp_path / "mine.png"
        PILImage.new("RGBA", (64, 64), (255, 255, 255, 255)).save(custom)
        added = _add(harness, "custom", icon=str(custom))
        assert added["icon"] == str(custom)
    finally:
        harness.stop()


def test_an_unknown_entry_is_not_found() -> None:
    harness = _harness()
    try:
        with pytest.raises(CommandError) as excinfo:
            harness.command("entry set", id="missing", label="X")
        assert excinfo.value.code == "not_found"
        with pytest.raises(CommandError) as excinfo:
            harness.command("entry remove", id="missing")
        assert excinfo.value.code == "not_found"
    finally:
        harness.stop()


def test_a_duplicate_entry_id_is_refused() -> None:
    harness = _harness()
    try:
        _add(harness, "mail")
        with pytest.raises(CommandError) as excinfo:
            _add(harness, "mail")
        assert excinfo.value.code == "invalid_input"
        assert "mail" in str(excinfo.value.message)
    finally:
        harness.stop()


# ---------------------------------------------------------------- count set


def test_count_set_publishes_the_count_on_the_entry_topic_with_a_ttl() -> None:
    harness = _harness()
    try:
        _add(harness, "mail", topic="count.mail")
        result = harness.command("count set", id="mail", value="1204")
        assert result["id"] == "mail"
        assert result["topic"] == "count.mail"
        assert result["value"] == 1204
        assert isinstance(result["value"], int)

        sample = harness.bus.get("count.mail")
        assert sample.value == 1204
        assert str(sample.freshness) == "fresh"
        assert result["ttl_s"] > 0, "the count is set with a TTL"

        # The TTL is the entry's own staleness limit: once it passes the topic
        # reads stale and the chip shows the unknown state.
        harness.clock.advance(result["ttl_s"] + 1.0)
        assert str(harness.bus.get("count.mail").freshness) == "stale"
        harness.render()
        assert harness.state()["entries"][0]["shown"] == STALE_TEXT
    finally:
        harness.stop()


def test_count_set_refuses_a_non_integer_count_and_an_unknown_id() -> None:
    harness = _harness()
    try:
        _add(harness, "mail")
        for bad in ("abc", "", "12.5", "1e3", None, True):
            with pytest.raises(CommandError) as excinfo:
                harness.command("count set", id="mail", value=bad)
            assert excinfo.value.code == "invalid_input", bad
        with pytest.raises(CommandError) as excinfo:
            harness.command("count set", id="missing", value="1")
        assert excinfo.value.code == "not_found"
        # A negative count is a number but not a count.
        with pytest.raises(CommandError) as excinfo:
            harness.command("count set", id="mail", value="-1")
        assert excinfo.value.code == "invalid_input"
    finally:
        harness.stop()


# ------------------------------------------------------- the alert entry


def test_the_alert_entry_is_implicit_and_bound_to_alerts_missed() -> None:
    harness = _harness(entries=[{"id": "mail", "icon": "mail", "topic": "count.mail"}])
    try:
        state = harness.state()
        assert [entry["id"] for entry in state["entries"]] == ["mail"]
        assert state["alert"]["id"] == "alert"
        assert state["alert"]["topic"] == ALERT_TOPIC
        assert state["alert"]["shown"] is None, "the alert entry is hidden at zero"

        harness.bus.publish(ALERT_TOPIC, {"count": 2, "max_severity": "critical"})
        state = harness.state()
        assert state["alert"]["count"] == 2
        assert state["alert"]["max_severity"] == "critical"
        assert state["alert"]["shown"] == "2"
    finally:
        harness.stop()


def test_the_plugin_subscribes_to_its_topics_and_requests_a_render() -> None:
    harness = _harness(entries=[{"id": "mail", "icon": "mail", "topic": "count.mail"}])
    try:
        renders: list[int] = []
        harness.ctx.request_render_handler = lambda: renders.append(1)
        harness.bus.publish("count.mail", 3)
        assert renders, "publishing a sidebar topic must call ctx.request_render()"
        harness.bus.publish(ALERT_TOPIC, {"count": 1, "max_severity": "info"})
        assert len(renders) >= 2, "the alert topic is subscribed too"
    finally:
        harness.stop()


# ------------------------------------------------------------------ settings


def test_a_settings_change_is_adopted_live() -> None:
    harness = _harness()
    try:
        changed = harness.set_settings(
            entries=[{"id": "chat", "icon": "chat", "label": "Chat", "topic": "count.chat"}]
        )
        assert len(changed["entries"]) == 1
        assert [entry["id"] for entry in harness.state()["entries"]] == ["chat"]
    finally:
        harness.stop()


def test_a_settings_change_with_too_many_entries_is_rejected() -> None:
    harness = _harness()
    try:
        with pytest.raises(SettingsRejected) as excinfo:
            harness.set_settings(
                entries=[
                    {"id": f"e{index}", "icon": "mail", "topic": f"count.e{index}"}
                    for index in range(MAX_ENTRIES)
                ]
            )
        assert "6" in str(excinfo.value)
    finally:
        harness.stop()


def test_an_unusable_icon_in_settings_is_skipped_not_fatal() -> None:
    harness = _harness(
        entries=[
            {"id": "ok", "icon": "mail", "topic": "count.ok"},
            {"id": "", "icon": "mail", "topic": "count.bad"},
        ]
    )
    try:
        assert [entry["id"] for entry in harness.state()["entries"]] == ["ok"]
    finally:
        harness.stop()


def test_the_default_config_registers_the_sidebar_with_an_empty_entry_list() -> None:
    """T109: a fresh install has the sidebar instance and all four built-ins."""
    from smart_panel.core.config import initial_config
    from smart_panel.plugins import builtin_instances, builtin_plugins

    assert "sidebar" in builtin_plugins()
    instances = builtin_instances()
    assert instances["sidebar"]["plugin"] == "sidebar"
    assert instances["sidebar"]["enabled"] is True
    assert instances["sidebar"]["settings"]["entries"] == []

    config = initial_config()
    sidebar = [
        region for region in config["layout"]["regions"] if region["name"] == "sidebar"
    ]
    assert sidebar and sidebar[0]["instance"] == "sidebar"
    assert {region["instance"] for region in config["layout"]["regions"]} == {
        "sidebar",
        "gauges",
        "crawl",
    }
    assert {overlay["instance"] for overlay in config["layout"]["overlays"]} == {"alerts"}

    harness = PluginHarness(SidebarPlugin, instances["sidebar"]["settings"])
    try:
        assert harness.state()["count"] == 0
        assert harness.state()["max"] == MAX_ENTRIES
    finally:
        harness.stop()


def test_a_sidecar_error_type_exists_for_the_plugin() -> None:
    assert issubclass(SidebarError, ValueError)
