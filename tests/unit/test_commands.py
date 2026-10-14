"""Unit regressions for the control command handlers (T052).

F3 — a validation failure must surface as ``invalid_input`` (CLI exit 2), not
``internal`` (exit 1). ``SettingError`` and ``SettingsRejected`` were already
mapped, but ``LayoutError`` and ``SecretError`` are plain ``ValueError``s and
fell through to the catch-all: an invalid layout or secret name was reported as
an internal error instead of a usage error.

F4 — ``logs.tail --follow`` must keep streaming once the log ring is full. The
ring is a fixed-size ``deque``, so its length stops growing at capacity; a
follow loop that indexes new events by the *length* of the ring silently drops
every event from then on. The stream tracks the log's monotonic sequence
number instead.

F5 — ``plugin.instance.add`` used to accept a ``--plugin`` name the loader never
discovered: the instance was persisted straight through ``set_config``, and on
the next apply ``Runner._start_instance`` found no ``plugin_class`` and logged
``instance_skipped`` forever — a "dead" instance that ``plugin instance list``
still reported. FR-006 requires the change to be validated before it is applied,
so the handler rejects the unknown name (``invalid_input``, CLI exit 2) naming
the setting and persists nothing.

F5 followup — the generic dotted-path setter had the same hole: ``config set
plugins.<name> '{...}'`` (or a dotted sub-key such as ``plugins.<name>.plugin``)
persisted an instance whose ``plugin`` the loader never discovered. FR-006
requires the change to be validated before it is applied and ``contracts/cli.md``
puts "unknown plugin" at exit 2 for the CLI as a whole, so ``config set`` now
rejects it with the same ``invalid_input`` / ``plugins.<name>.plugin`` message
as ``plugin.instance.add`` — the loader's ``instance_skipped`` warning stays the
backstop for a hand-edited ``config.json``.

F5 ordering (the wave-3 carry-over) — nothing pinned *when* ``plugin.instance.add``
runs the shared guard relative to the settings coercion. A mutation that moved
the guard after ``dict(args.get("settings") or {})`` left the whole suite green
while changing which error a caller saw: "unknown plugin **and** malformed
settings" reported the coercion's setting (``plugins.dead.settings``) instead of
the guard's (``plugins.dead.plugin``). The ordering is now pinned, so the plugin
refusal is always the error a caller sees.

F5 settings type — a malformed ``settings`` *type* on a plugin the loader knows
(``["not-a-pair"]``, which ``dict(...)`` cannot consume) used to fall through to
the dispatch catch-all as ``internal`` (exit 1). FR-006 says an invalid change is
rejected with a message that identifies the setting, so the handler now maps the
coercion's failure to ``invalid_input`` (exit 2) naming ``plugins.<name>.settings``
— the same class the other F3 validators use.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from smart_panel.core import commands
from smart_panel.core.commands import _logs_tail, make_dispatch
from smart_panel.core.config import validate_config
from smart_panel.core.control import protocol
from smart_panel.core.logging import RunnerLog
from smart_panel.core.plugins import PluginLoader
from smart_panel.core.secrets import SecretError
from smart_panel.sdk.plugin import Manifest, Plugin


class _NullLog:
    """A logger that discards everything."""

    def debug(self, event: str, **fields: object) -> None:
        pass

    info = debug
    warning = debug
    error = debug


class _StubSecrets:
    """A secrets store whose ``set`` validates the name like the real one."""

    def set(self, name: str, value: object) -> None:
        raise SecretError(f"secret name {name!r} must match [A-Za-z0-9_.-]+")

    def list_names(self) -> list[str]:
        return []


class _StubRunner:
    """A duck-typed runner with real config validation and no threads or socket."""

    def __init__(self, log: object | None = None, loader: object | None = None) -> None:
        self._config = validate_config(
            {
                "display": {"fps": 15, "brightness": 50, "sink": "null", "min_text_px": 24},
                "layout": {"regions": [], "overlays": []},
                "plugins": {},
            }
        )
        self.log = log if log is not None else _NullLog()
        self.loader = loader
        self.stopping = False
        self.secrets = _StubSecrets()

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
        # Real validation, so a bad layout raises the real LayoutError.
        self._config = validate_config(candidate)
        return {"path": dotted, "value": value, "applied": True}


class _FollowRunner:
    """A runner stub whose ``stopping`` flips after a bounded number of polls.

    A correct stream yields the new event on the first poll; a broken one that
    never yields would otherwise spin forever, so the budget makes it end.
    """

    def __init__(self, log: RunnerLog, poll_budget: int = 6) -> None:
        self.log = log
        self._remaining = poll_budget

    @property
    def stopping(self) -> bool:
        self._remaining -= 1
        return self._remaining < 0


# ------------------------------------------------------------------ F3: mapping


def test_an_undersized_region_is_reported_as_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "layout.region.set",
                {"name": "main", "x": 0, "y": 0, "w": 10, "h": 10, "instance": "text"},
            )
        )
    assert excinfo.value.code == "invalid_input"
    assert "main" in excinfo.value.message


def test_an_overlay_on_a_missing_region_is_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(protocol.Request("layout.overlay.add", {"region": "ghost", "instance": "text"}))
    assert excinfo.value.code == "invalid_input"


def test_an_invalid_secret_name_is_invalid_input() -> None:
    dispatch = make_dispatch(_StubRunner())
    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(protocol.Request("secret.set", {"name": "bad name", "value": "x"}))
    assert excinfo.value.code == "invalid_input"


# ------------------------------------------------------------------ F5: add


class _TinyRegionPlugin(Plugin):
    """A loader-visible stand-in, so the accepted path has one known plugin."""

    manifest = Manifest(name="text", version="0.1.0", capabilities=frozenset({"region"}))


def test_adding_an_instance_for_an_unknown_plugin_is_invalid_input(tmp_path: Path) -> None:
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    dispatch = make_dispatch(_StubRunner(loader=loader))

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "plugin.instance.add", {"name": "dead", "plugin": "no-such-plugin"}
            )
        )

    assert excinfo.value.code == "invalid_input"
    assert "no-such-plugin" in excinfo.value.message
    # FR-006: the message identifies the setting.
    assert excinfo.value.setting == "plugins.dead.plugin"


def test_adding_an_instance_for_an_unknown_plugin_persists_nothing(tmp_path: Path) -> None:
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    with pytest.raises(protocol.ProtocolError):
        dispatch(
            protocol.Request(
                "plugin.instance.add", {"name": "dead", "plugin": "no-such-plugin"}
            )
        )

    # No dead instance: the config was never written through set_config.
    assert runner.config_data["plugins"] == {}


def test_adding_an_instance_for_a_discovered_plugin_is_accepted(tmp_path: Path) -> None:
    loader = PluginLoader(builtins={"text": _TinyRegionPlugin}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    response = dispatch(
        protocol.Request("plugin.instance.add", {"name": "hello", "plugin": "text"})
    )

    assert response.ok
    assert runner.config_data["plugins"]["hello"]["plugin"] == "text"


def test_adding_an_instance_runs_the_guard_before_coercing_settings(tmp_path: Path) -> None:
    # FR-006 ordering: the plugin guard runs BEFORE the settings are coerced.
    # Two dispatches share the same malformed ``settings`` (``["not-a-pair"]``,
    # which ``dict(...)`` cannot consume) and differ only in whether the loader
    # knows the plugin. Pinning *both* outcomes is what makes the ordering
    # observable: with the guard first the unknown plugin is refused by the guard
    # (outcome 1, its ``plugins.dead.plugin`` setting); were the guard after
    # ``dict(args.get("settings") or {})``, that same dispatch would report the
    # coercion's ``plugins.dead.settings`` instead — the guard's actionable
    # plugin refusal would be masked by the settings verdict. Both outcomes are
    # now ``invalid_input`` (exit 2) after the F5 settings-type fix below.
    loader = PluginLoader(builtins={"text": _TinyRegionPlugin}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)
    malformed = ["not-a-pair"]

    # Outcome 1 -- unknown plugin: the guard's verdict, naming the setting and the
    # offending plugin, with nothing persisted.
    with pytest.raises(protocol.ProtocolError) as guard:
        dispatch(
            protocol.Request(
                "plugin.instance.add",
                {"name": "dead", "plugin": "no-such-plugin", "settings": malformed},
            )
        )
    assert guard.value.code == "invalid_input"
    assert guard.value.setting == "plugins.dead.plugin"
    assert "no-such-plugin" in guard.value.message
    assert runner.config_data["plugins"] == {}

    # Outcome 2 -- a plugin the loader knows, same malformed settings: the guard
    # passes, so the coercion is what fails. The outcome is the coercion's, not the
    # guard's: ``invalid_input`` (exit 2) naming ``plugins.ok.settings`` (FR-006 —
    # an invalid change is rejected with a message that identifies the setting).
    # This contrast with outcome 1's ``plugins.dead.plugin`` is what pins the
    # ordering: run the guard *after* the coercion and outcome 1 would report the
    # settings verdict instead of the plugin one.
    with pytest.raises(protocol.ProtocolError) as coerced:
        dispatch(
            protocol.Request(
                "plugin.instance.add",
                {"name": "ok", "plugin": "text", "settings": malformed},
            )
        )
    assert coerced.value.code == "invalid_input"
    assert coerced.value.setting == "plugins.ok.settings"
    assert runner.config_data["plugins"] == {}


# ------------------------------------- F5 followup: config set plugins.<name>


def test_config_set_of_an_instance_for_an_unknown_plugin_is_invalid_input(
    tmp_path: Path,
) -> None:
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    dispatch = make_dispatch(_StubRunner(loader=loader))

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "config.set",
                {
                    "path": "plugins.dead",
                    "value": {"plugin": "no-such-plugin", "enabled": True, "settings": {}},
                },
            )
        )

    assert excinfo.value.code == "invalid_input"
    assert "no-such-plugin" in excinfo.value.message
    # FR-006: the message identifies the setting, exactly as plugin.instance.add.
    assert excinfo.value.setting == "plugins.dead.plugin"


def test_config_set_of_an_instance_for_an_unknown_plugin_persists_nothing(
    tmp_path: Path,
) -> None:
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    with pytest.raises(protocol.ProtocolError):
        dispatch(
            protocol.Request(
                "config.set",
                {"path": "plugins.dead", "value": {"plugin": "no-such-plugin"}},
            )
        )

    # No dead instance: the config was never written through set_config.
    assert runner.config_data["plugins"] == {}


def test_config_set_creating_a_dead_instance_through_a_dotted_key_is_rejected(
    tmp_path: Path,
) -> None:
    # plugins.dead.plugin on its own passes structural validation (enabled
    # defaults to True, settings to {}), so the guard must inspect the resulting
    # subtree rather than only a whole-instance value.
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request(
                "config.set", {"path": "plugins.dead.plugin", "value": "no-such-plugin"}
            )
        )

    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.dead.plugin"
    assert runner.config_data["plugins"] == {}


def test_config_set_of_a_dead_instance_left_by_a_hand_edited_config_is_rejected(
    tmp_path: Path,
) -> None:
    # A config.json can still be hand-edited and reloaded; the runner then only
    # logs instance_skipped for that entry. config set must not carry it forward.
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    runner.set_config("plugins.dead", {"plugin": "no-such-plugin", "enabled": True})
    dispatch = make_dispatch(runner)

    with pytest.raises(protocol.ProtocolError) as excinfo:
        dispatch(
            protocol.Request("config.set", {"path": "plugins.dead.enabled", "value": False})
        )

    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "plugins.dead.plugin"


def test_config_set_of_an_instance_for_a_discovered_plugin_is_accepted(
    tmp_path: Path,
) -> None:
    loader = PluginLoader(builtins={"text": _TinyRegionPlugin}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    response = dispatch(
        protocol.Request(
            "config.set",
            {"path": "plugins.hello", "value": {"plugin": "text", "enabled": True}},
        )
    )

    assert response.ok
    assert runner.config_data["plugins"]["hello"]["plugin"] == "text"


def test_config_set_outside_plugins_is_unaffected_by_the_guard(tmp_path: Path) -> None:
    loader = PluginLoader(builtins={}, home=tmp_path / "plugins")
    runner = _StubRunner(loader=loader)
    dispatch = make_dispatch(runner)

    response = dispatch(protocol.Request("config.set", {"path": "display.fps", "value": 20}))

    assert response.ok
    assert runner.config_data["display"]["fps"] == 20


def test_config_set_plugins_is_permissive_without_a_loader() -> None:
    # Mirrors plugin.instance.add: with no loader there is nothing to validate
    # against, so the write is attempted (a stub or detached runner has no
    # discovery to consult).
    runner = _StubRunner()  # loader is None
    dispatch = make_dispatch(runner)

    response = dispatch(
        protocol.Request(
            "config.set", {"path": "plugins.dead", "value": {"plugin": "no-such-plugin"}}
        )
    )

    assert response.ok
    assert runner.config_data["plugins"]["dead"]["plugin"] == "no-such-plugin"


# --------------------------------------------------------------- F4: logs.tail


def test_logs_tail_follow_streams_new_events_once_the_ring_is_full(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)

    log = RunnerLog(path=tmp_path / "runner.log", ring_size=4)
    for index in range(4):
        log.info("filler", n=index)
    runner = _FollowRunner(log)

    stream = _logs_tail(runner, {"follow": True, "lines": 1})
    assert next(stream)["n"] == 3  # the one-line backlog

    log.info("fresh", n=99)  # the ring is now full; its length will not grow
    received = False
    for _ in range(20):
        try:
            event = next(stream)
        except StopIteration:
            break
        if event.get("log_event") == "fresh":
            received = True
            break
    assert received, "a new log event was never streamed once the ring was full"
