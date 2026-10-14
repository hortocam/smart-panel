"""Contract tests for the gauge verbs (T093, US4).

The shapes come from ``contracts/builtin-plugins.md`` (the ``gauges`` section)
and ``data-model.md`` (Gauge): the verbs ``add``, ``set``, ``remove``, ``list``
and ``push``; a seventh gauge exits 2 with a message naming the maximum of 6;
``gauge push`` is the only spoolable verb; and an invalid ``range`` (min not
below max), thresholds out of order or outside the range, and a ``window_s``
outside 10..86400 are each refused as ``invalid_input`` (exit 2).
"""

from __future__ import annotations

import pytest

from smart_panel.plugins.gauges import GaugesPlugin
from smart_panel.plugins.gauges.model import MAX_GAUGES, Gauge
from smart_panel.sdk.plugin import CommandError, SettingsRejected
from smart_panel.sdk.settings import SettingError, validate
from smart_panel.sdk.testing import PluginHarness, assert_conforms

GAUGE_VERBS = {"add", "set", "remove", "list", "push"}
SPOOLABLE_VERBS = {"push"}


def _command(name: str):
    for command in GaugesPlugin.manifest.commands:
        if command.name == name:
            return command
    raise AssertionError(f"no command named {name!r}")


def _arg_names(name: str) -> set[str]:
    return {setting.name for setting in _command(name).args}


def _harness(**settings: object) -> PluginHarness:
    return PluginHarness(GaugesPlugin, settings, size=(1728, 382))


def _add(harness: PluginHarness, identifier: str, **overrides: object) -> dict:
    fields: dict = {
        "id": identifier,
        "label": identifier.upper(),
        "topic": f"sys.{identifier}",
        "aggregation": "avg",
        "window": 60,
        "style": "dial",
    }
    fields.update(overrides)
    return harness.command("add", **fields)


# ------------------------------------------------------------------- manifest


def test_the_manifest_declares_a_region_plugin_with_a_six_gauge_limit() -> None:
    manifest = GaugesPlugin.manifest
    assert manifest.name == "gauges"
    assert manifest.capabilities == frozenset({"region"})
    assert manifest.animated is False
    assert manifest.source_types == ()
    assert MAX_GAUGES == 6


def test_every_documented_verb_is_declared_with_its_arguments() -> None:
    assert {command.name for command in GaugesPlugin.manifest.commands} == GAUGE_VERBS
    assert _arg_names("add") >= {
        "id",
        "label",
        "topic",
        "aggregation",
        "window",
        "style",
        "unit",
        "min",
        "max",
        "warn",
        "crit",
    }
    assert _arg_names("set") >= {"id", "label", "aggregation", "style", "window"}
    assert _arg_names("remove") >= {"id"}
    assert _arg_names("list") == set()
    assert _arg_names("push") >= {"id", "value"}


def test_only_gauge_push_is_spoolable() -> None:
    for name in GAUGE_VERBS:
        assert _command(name).spoolable is (name in SPOOLABLE_VERBS), name


def test_the_plugin_conforms_to_the_sdk() -> None:
    assert_conforms(GaugesPlugin)


# ---------------------------------------------------------------- add/set/list


def test_add_set_remove_and_list_round_trip() -> None:
    harness = _harness()
    try:
        added = _add(harness, "cpu", label="CPU", topic="sys.cpu", unit="%", warn=75, crit=90)
        assert added["id"] == "cpu"
        assert added["label"] == "CPU"
        assert added["style"] == "dial"
        assert added["aggregation"] == "avg"
        assert added["window_s"] == 60
        assert added["unit"] == "%"
        assert added["range"] == [0, 100]
        assert added["thresholds"] == [
            {"at": 75, "color": "#E6A700"},
            {"at": 90, "color": "#D62839"},
        ]
        assert added["count"] == 1

        listed = harness.command("list")
        assert listed["count"] == 1
        assert [gauge["id"] for gauge in listed["gauges"]] == ["cpu"]
        assert listed["max"] == MAX_GAUGES

        changed = harness.command("set", id="cpu", label="Processor", style="bar", min=0, max=200)
        assert changed["label"] == "Processor"
        assert changed["style"] == "bar"
        assert changed["range"] == [0, 200]

        removed = harness.command("remove", id="cpu")
        assert removed == {"id": "cpu", "removed": True, "count": 0}
        assert harness.command("list")["gauges"] == []
    finally:
        harness.stop()


def test_a_seventh_gauge_is_rejected_naming_the_maximum_of_six() -> None:
    harness = _harness()
    try:
        for index in range(MAX_GAUGES):
            _add(harness, f"g{index}")
        assert harness.command("list")["count"] == MAX_GAUGES

        with pytest.raises(CommandError) as excinfo:
            _add(harness, "g7")
        assert excinfo.value.code == "invalid_input"
        message = str(excinfo.value.message)
        assert "6" in message, message
        assert "maximum" in message.lower(), message
        assert len(harness.command("list")["gauges"]) == MAX_GAUGES
    finally:
        harness.stop()


def test_an_unknown_gauge_is_not_found() -> None:
    harness = _harness()
    try:
        with pytest.raises(CommandError) as excinfo:
            harness.command("set", id="missing", label="X")
        assert excinfo.value.code == "not_found"
        with pytest.raises(CommandError) as excinfo:
            harness.command("remove", id="missing")
        assert excinfo.value.code == "not_found"
    finally:
        harness.stop()


def test_a_duplicate_gauge_id_is_refused() -> None:
    harness = _harness()
    try:
        _add(harness, "cpu")
        with pytest.raises(CommandError) as excinfo:
            _add(harness, "cpu")
        assert excinfo.value.code == "invalid_input"
        assert "cpu" in str(excinfo.value.message)
    finally:
        harness.stop()


# ------------------------------------------------------------------ validation


def test_an_invalid_range_is_refused() -> None:
    harness = _harness()
    try:
        for overrides in ({"min": 100, "max": 100}, {"min": 200, "max": 100}):
            with pytest.raises(CommandError) as excinfo:
                _add(harness, "bad", **overrides)
            assert excinfo.value.code == "invalid_input"
            assert "range" in str(excinfo.value.message)
    finally:
        harness.stop()


def test_thresholds_must_ascend_and_sit_inside_the_range() -> None:
    harness = _harness()
    try:
        for overrides in (
            {"warn": 90, "crit": 75},  # out of order
            {"warn": 75, "crit": 75},  # not strictly ascending
            {"warn": -1},  # below the range
            {"crit": 101},  # above the range
        ):
            with pytest.raises(CommandError) as excinfo:
                _add(harness, "bad", **overrides)
            assert excinfo.value.code == "invalid_input", overrides
            assert "threshold" in str(excinfo.value.message), overrides
    finally:
        harness.stop()


def test_a_window_outside_ten_to_86400_seconds_is_refused() -> None:
    harness = _harness()
    try:
        for window in (0, 9, 86401, -60):
            with pytest.raises(CommandError) as excinfo:
                _add(harness, "bad", window=window)
            assert excinfo.value.code == "invalid_input", window
            assert "window_s" in str(excinfo.value.message), window
        assert _add(harness, "ten", window=10)["window_s"] == 10
        assert _add(harness, "day", window=86400)["window_s"] == 86400
    finally:
        harness.stop()


def test_a_label_or_unit_over_its_limit_and_a_bad_style_or_aggregation_are_refused() -> None:
    harness = _harness()
    try:
        for overrides in (
            {"label": "x" * 25},
            {"unit": "1234567"},
            {"style": "pie"},
            {"aggregation": "median"},
            {"id": ""},
        ):
            with pytest.raises(CommandError) as excinfo:
                _add(harness, "bad", **overrides)
            assert excinfo.value.code == "invalid_input", overrides
    finally:
        harness.stop()


# -------------------------------------------------------------------- push


def test_push_publishes_the_value_on_the_push_topic() -> None:
    harness = _harness()
    try:
        _add(harness, "q", topic="push.q", aggregation="max", window=300, style="bar")
        result = harness.command("push", id="q", value=42)
        assert result["id"] == "q"
        assert result["topic"] == "push.q"
        assert result["value"] == 42
        sample = harness.bus.get("push.q")
        assert sample.value == 42
        assert str(sample.freshness) == "fresh", (
            "the gauge's own stale_after_s is the limit, not a bus TTL (FR-032)"
        )
    finally:
        harness.stop()


def test_push_refuses_an_unknown_id_and_a_non_numeric_value() -> None:
    harness = _harness()
    try:
        _add(harness, "q", topic="push.q")
        with pytest.raises(CommandError) as excinfo:
            harness.command("push", id="missing", value=1)
        assert excinfo.value.code == "not_found"
        for bad in ("hot", None, True, [1]):
            with pytest.raises(CommandError) as excinfo:
                harness.command("push", id="q", value=bad)
            assert excinfo.value.code == "invalid_input", bad
    finally:
        harness.stop()


# ------------------------------------------------------------------ settings


def test_the_declared_settings_match_the_contract() -> None:
    settings = validate(GaugesPlugin.manifest.settings, {})
    assert settings["gauges"] == []
    assert settings["layout"] == "auto"
    assert settings["bg"] == "#101418"
    assert settings["value_size"] == 72
    assert settings["label_size"] == 28

    with pytest.raises(SettingError):
        validate(GaugesPlugin.manifest.settings, {"bg": "not-a-colour"})
    with pytest.raises(SettingError):
        validate(GaugesPlugin.manifest.settings, {"value_size": "big"})
    with pytest.raises(SettingError):
        validate(GaugesPlugin.manifest.settings, {"unknown": 1})
    # The contract's ``{cols, rows}`` form of ``layout`` is accepted as the JSON
    # object string the SDK's ``str`` setting can carry.
    assert validate(GaugesPlugin.manifest.settings, {"layout": '{"cols": 3, "rows": 2}'})[
        "layout"
    ] == '{"cols": 3, "rows": 2}'


def test_a_settings_change_is_adopted_live() -> None:
    harness = _harness()
    try:
        changed = harness.set_settings(
            gauges=[
                {
                    "id": "cpu",
                    "label": "CPU",
                    "topic": "sys.cpu",
                    "aggregation": "avg",
                    "window_s": 60,
                    "style": "dial",
                }
            ]
        )
        assert len(changed["gauges"]) == 1
        assert harness.plugin.settings["gauges"][0]["id"] == "cpu"
        assert [gauge["id"] for gauge in harness.state()["gauges"]] == ["cpu"]
    finally:
        harness.stop()


def test_a_settings_change_with_a_seventh_gauge_is_rejected() -> None:
    harness = _harness()
    try:
        seven = [
            {"id": f"g{index}", "label": "G", "topic": f"sys.{index}", "style": "numeric"}
            for index in range(MAX_GAUGES + 1)
        ]
        with pytest.raises(SettingsRejected) as excinfo:
            harness.set_settings(gauges=seven)
        assert "6" in str(excinfo.value)
    finally:
        harness.stop()


def test_the_plugin_subscribes_to_its_topics_and_requests_a_render() -> None:
    """T099: a change on a subscribed topic asks the runner for a re-render."""
    harness = _harness()
    try:
        renders: list[int] = []
        harness.ctx.request_render_handler = lambda: renders.append(1)
        _add(harness, "cpu", topic="sys.cpu")
        harness.bus.publish("sys.cpu", 55.0)
        assert renders, "publishing a gauge topic must call ctx.request_render()"
    finally:
        harness.stop()


def test_a_never_published_topic_reads_as_no_data() -> None:
    harness = _harness(
        gauges=[{"id": "cpu", "label": "CPU", "topic": "sys.cpu", "style": "numeric"}]
    )
    try:
        image = harness.render(0.0)
        assert image is not None and image.size == (1728, 382)
        detail = harness.state()["gauges"][0]
        assert detail["id"] == "cpu"
        assert detail["value"] is None
        assert detail["stale"] is True
        assert detail["freshness"] == "no_data"
    finally:
        harness.stop()


def test_a_gauge_goes_stale_after_three_times_its_observed_interval() -> None:
    """FR-032/FR-036: the source's interval belongs to the source, so it is observed."""
    harness = _harness(
        gauges=[{"id": "cpu", "label": "CPU", "topic": "sys.cpu", "style": "numeric"}]
    )
    try:
        harness.bus.publish("sys.cpu", 10.0)
        harness.render()
        harness.clock.advance(10.0)  # the source refreshes every 10 s
        harness.bus.publish("sys.cpu", 20.0)
        harness.render()
        assert harness.state()["gauges"][0]["stale"] is False

        # 3 x the observed 10 s interval is 30 s: 20 s without a sample is still fresh...
        harness.clock.advance(20.0)
        harness.render()
        detail = harness.state()["gauges"][0]
        assert detail["stale"] is False
        assert detail["value"] == pytest.approx(20.0), (
            "the last known value stays visible until the staleness limit passes"
        )

        # ...and one more second tips it into the stale state.
        harness.clock.advance(11.0)
        harness.render()
        detail = harness.state()["gauges"][0]
        assert detail["stale"] is True
        assert detail["shown"] == "no data"
    finally:
        harness.stop()


def test_an_explicit_stale_after_overrides_the_derived_interval() -> None:
    harness = _harness(
        gauges=[
            {
                "id": "x",
                "label": "X",
                "topic": "push.x",
                "style": "numeric",
                "stale_after_s": 2,
            }
        ]
    )
    try:
        harness.bus.publish("push.x", 5.0)
        harness.render()
        assert harness.state()["gauges"][0]["stale"] is False
        harness.clock.advance(2.1)
        harness.render()
        assert harness.state()["gauges"][0]["stale"] is True
    finally:
        harness.stop()


# ------------------------------------------------------- the default config


def test_the_default_config_has_a_sys_source_and_four_gauges_in_main() -> None:
    """T100: a fresh install starts with the US4 defaults (FR-033)."""
    from smart_panel.core.config import initial_config
    from smart_panel.plugins import builtin_instances, builtin_plugins

    assert "gauges" in builtin_plugins()

    instances = builtin_instances()
    sources = instances["sources"]["settings"]["sources"]
    assert sources["sys"]["type"] == "system"
    assert sources["sys"]["interval_s"] == 2
    assert sources["sys"]["settings"]["metrics"] == ["cpu", "mem", "temp", "disk:/"]

    default_gauges = instances["gauges"]["settings"]["gauges"]
    assert [gauge["id"] for gauge in default_gauges] == ["cpu", "mem", "temp", "disk"]
    assert [gauge["style"] for gauge in default_gauges] == ["dial", "bar", "numeric", "sparkline"]
    for gauge in default_gauges:
        Gauge.from_dict(gauge)  # every default passes the same validation as ``gauge add``

    # The assembled default config is valid and puts the gauges in ``main``.
    config = initial_config()
    main = [region for region in config["layout"]["regions"] if region["name"] == "main"]
    assert main and main[0]["instance"] == "gauges"
    assert config["plugins"]["gauges"]["settings"]["gauges"] == default_gauges

    harness = PluginHarness(GaugesPlugin, instances["gauges"]["settings"])
    try:
        assert harness.state()["count"] == len(default_gauges)
    finally:
        harness.stop()
