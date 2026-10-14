"""Contract tests for plugin management in the core and CLI (T115, US6).

The plugin-facing surface ``contracts/cli.md`` pins (FR-046): ``plugin list``
reports a plugin's state (including ``load_failed`` and ``incompatible``),
``plugin describe`` prints its manifest settings with their types, ranges and
defaults, an invalid setting is rejected with the plugin's **declared**
constraint quoted, and ``schema`` lists third-party plugin verbs alongside the
core commands.

The handlers are driven through ``make_dispatch`` (the CLI's server side) with a
loader and a stub runner, so the verdicts are exactly the ones a caller sees.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from smart_panel.core import commands
from smart_panel.core.commands import make_dispatch
from smart_panel.core.config import validate_config
from smart_panel.core.control import protocol
from smart_panel.core.plugins import PluginLoader
from smart_panel.plugins import BUILTIN_PLUGINS
from smart_panel.sdk.plugin import Command, Manifest, Plugin
from smart_panel.sdk.settings import Setting


class _NullLog:
    def debug(self, event: str, **fields: object) -> None:
        pass

    info = debug
    warning = debug
    error = debug


class _StubRunner:
    """A duck-typed runner with real config validation and a real loader."""

    def __init__(self, loader: PluginLoader | None = None) -> None:
        self._config = validate_config(
            {
                "display": {"fps": 15, "brightness": 50, "sink": "null"},
                "layout": {"regions": [], "overlays": []},
                "plugins": {},
            }
        )
        self.loader = loader
        self.log = _NullLog()
        self.stopping = False

    @property
    def config_data(self) -> dict:
        return copy.deepcopy(self._config)

    def set_config(self, dotted: str, value: object) -> dict:
        candidate = copy.deepcopy(self._config)
        node = candidate
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        self._config = validate_config(candidate)
        return {"path": dotted, "value": value, "applied": True}


class _Speedster(Plugin):
    """A third-party plugin with declared settings, ranges and a verb."""

    manifest = Manifest(
        name="speedster",
        version="1.2.0",
        sdk_version="1",
        summary="a third-party plugin with declared settings",
        capabilities=frozenset({"region"}),
        settings=(
            Setting(
                name="speed",
                type="int",
                default=120,
                description="Pixels per second",
                min=10,
                max=600,
            ),
            Setting(
                name="mode",
                type="enum",
                default="fast",
                description="Scroll mode",
                choices=["slow", "fast"],
            ),
        ),
        commands=(Command(name="boost", summary="Raise the speed"),),
    )


def _loader(tmp_path: Path, *, builtins: dict | None = None) -> PluginLoader:
    return PluginLoader(builtins=builtins or {}, home=tmp_path / "plugins")


def _dispatch(runner: _StubRunner):
    return make_dispatch(runner)


def _write(plugin_dir: Path, filename: str, text: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / filename).write_text(text, encoding="utf-8")


SAMPLE = (
    "from smart_panel.sdk.plugin import Manifest, Plugin\n"
    "class Sample(Plugin):\n"
    '    manifest = Manifest(name="sample", summary="a sample")\n'
)
BROKEN = "import definitely_not_a_real_module_xyz  # noqa: F401\n"
FUTURE = (
    "from smart_panel.sdk.plugin import Manifest, Plugin\n"
    "class Future(Plugin):\n"
    '    manifest = Manifest(name="future", sdk_version="2", summary="targets a newer SDK")\n'
)


# ------------------------------------------------------------- plugin list


def test_plugin_list_reports_load_failed_and_incompatible_states(tmp_path: Path) -> None:
    home = tmp_path / "plugins"
    _write(home, "sample.py", SAMPLE)
    _write(home, "broken.py", BROKEN)
    _write(home, "future.py", FUTURE)
    runner = _StubRunner(_loader(tmp_path))
    dispatch = _dispatch(runner)

    response = dispatch(protocol.Request("plugin.list", {}))
    assert response.ok
    records = {record["name"]: record for record in response.data["plugins"]}

    assert records["sample"]["state"] == "loaded"
    assert records["broken"]["state"] == "load_failed"
    assert "definitely_not_a_real_module_xyz" in (records["broken"]["error"] or "")
    assert records["future"]["state"] == "incompatible"
    assert "SDK" in (records["future"]["error"] or "")


# --------------------------------------------------------- plugin describe


def test_plugin_describe_prints_settings_with_types_ranges_and_defaults() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)

    response = dispatch(protocol.Request("plugin.describe", {"name": "speedster"}))
    assert response.ok
    manifest = response.data["manifest"]
    assert manifest["version"] == "1.2.0"
    assert manifest["capabilities"] == ["region"]

    settings = {setting["name"]: setting for setting in manifest["settings"]}
    assert settings["speed"]["type"] == "int"
    assert settings["speed"]["default"] == 120
    assert settings["speed"]["min"] == 10 and settings["speed"]["max"] == 600
    assert settings["mode"]["type"] == "enum"
    assert list(settings["mode"]["choices"]) == ["slow", "fast"]

    assert [command["name"] for command in manifest["commands"]] == ["boost"]
    assert manifest["commands"][0]["summary"] == "Raise the speed"


def test_plugin_describe_of_an_unknown_plugin_is_not_found() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent")))
    dispatch = _dispatch(runner)
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(protocol.Request("plugin.describe", {"name": "ghost"}))
    assert excinfo.value.code == "not_found"


# ----------------------------------------------------- setting rejection


def test_an_out_of_range_setting_quotes_the_declared_maximum() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "plugin.instance.add",
                {"name": "s1", "plugin": "speedster", "settings": {"speed": 9999}},
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.s1.settings.speed"
    assert "600" in excinfo.value.message, "the declared maximum must be quoted"
    assert runner.config_data["plugins"] == {}, "a rejected change persists nothing"


def test_an_unknown_setting_is_rejected_naming_the_plugin_path() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "plugin.instance.add",
                {"name": "s1", "plugin": "speedster", "settings": {"nope": 1}},
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.s1.settings.nope"


def test_a_wrong_type_setting_is_rejected() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "plugin.instance.add",
                {"name": "s1", "plugin": "speedster", "settings": {"speed": "fast"}},
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.s1.settings.speed"


def test_conforming_settings_are_accepted() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)
    response = dispatch(
        protocol.Request(
            "plugin.instance.add",
            {"name": "s1", "plugin": "speedster", "settings": {"speed": 300, "mode": "slow"}},
        )
    )
    assert response.ok
    assert runner.config_data["plugins"]["s1"]["settings"]["speed"] == 300


def test_instance_set_validates_the_merged_settings() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    runner.set_config(
        "plugins.s1",
        {"plugin": "speedster", "enabled": True, "settings": {"speed": 300}},
    )
    dispatch = _dispatch(runner)

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request("plugin.instance.set", {"name": "s1", "settings": {"speed": 0}})
        )
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.s1.settings.speed"
    assert "10" in excinfo.value.message, "the declared minimum must be quoted"

    # A conforming change (keeping the existing value) is accepted.
    response = dispatch(
        protocol.Request("plugin.instance.set", {"name": "s1", "settings": {"speed": 250}})
    )
    assert response.ok
    assert runner.config_data["plugins"]["s1"]["settings"]["speed"] == 250


def test_config_set_on_a_plugins_path_validates_the_settings() -> None:
    runner = _StubRunner(_loader(Path("/nonexistent"), builtins={"speedster": _Speedster}))
    dispatch = _dispatch(runner)
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "config.set",
                {
                    "path": "plugins.s1",
                    "value": {"plugin": "speedster", "settings": {"speed": 9999}},
                },
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.s1.settings.speed"
    assert runner.config_data["plugins"] == {}


# ----------------------------------------------------------------- schema


def test_schema_includes_third_party_plugin_verbs() -> None:
    loader = _loader(Path("/nonexistent"), builtins={"speedster": _Speedster})
    document = commands.schema(SimpleNamespace(loader=loader))
    assert "speedster.boost" in document["plugin_verbs"]
    verb = document["plugin_verbs"]["speedster.boost"]
    assert verb["summary"] == "Raise the speed"
    assert verb["spoolable"] is False
    assert verb["args"] == []

    # The built-ins' verbs are in the same document, so an agent sees one surface.
    builtin_loader = _loader(Path("/nonexistent"), builtins=BUILTIN_PLUGINS)
    builtin_doc = commands.schema(SimpleNamespace(loader=builtin_loader))
    assert "text.set" in builtin_doc["plugin_verbs"]
