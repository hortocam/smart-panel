"""Control command handlers (T052).

The runner serves ``contracts/control-protocol.md`` by dispatching every request
to a handler here. A handler is a plain function ``(runner, args) -> data`` that
returns a JSON-compatible mapping (or, for ``logs.tail --follow``, an iterator
of event objects the server streams). Raising
:class:`~smart_panel.core.control.protocol.ProtocolError` — directly or through
one of the mapped exceptions — is how a handler reports a failure with a
documented error code.

Handlers read and mutate the running :class:`~smart_panel.core.runner.Runner`;
they are duck-typed on it so this module never imports ``runner`` (which imports
this module to build its dispatcher), avoiding a cycle.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict
from typing import Any

from smart_panel.core.config import assign
from smart_panel.core.control import protocol
from smart_panel.core.layout import LayoutError
from smart_panel.core.secrets import SecretError, SecretNotFound
from smart_panel.sdk.plugin import CommandError, SettingsRejected
from smart_panel.sdk.settings import Setting, SettingError

__all__ = ["HANDLERS", "make_dispatch", "schema"]

# exit-code name -> number, for ``schema.get``: the documented table plus the
# extra codes named in contracts/cli.md.
_EXIT_CODES: dict[str, int] = {name: code for code, name in protocol.EXIT_CODE_NAMES.items()}
_EXIT_CODES.update(protocol.EXIT_CODES)

# The command surface for ``schema.get`` — one entry per dispatchable command.
_CORE_COMMANDS: dict[str, dict[str, Any]] = {
    "runner.status": {"summary": "Runner state, frames, plugins and spool depth"},
    "runner.reload": {"summary": "Re-read config.json and apply it live"},
    "runner.stop": {"summary": "Stop the runner gracefully", "args": {"timeout_s": "float?"}},
    "config.get": {"summary": "Read one dotted-path setting", "args": {"path": "str"}},
    "config.show": {"summary": "Print the config (secrets redacted)", "args": {"path": "str?"}},
    "config.set": {
        "summary": "Set a dotted-path setting and apply it live",
        "args": {"path": "str", "value": "any"},
    },
    "config.unset": {"summary": "Reset a setting to its default", "args": {"path": "str"}},
    "config.validate": {"summary": "Validate a config document", "args": {"config": "object"}},
    "config.schema": {"summary": "The JSON schema of the core config and plugins"},
    "secret.set": {"summary": "Store a secret", "args": {"name": "str", "value": "str"}},
    "secret.delete": {"summary": "Delete a secret", "args": {"name": "str"}},
    "secret.list": {"summary": "List secret names (never values)"},
    "display.brightness.set": {
        "summary": "Set backlight brightness 0-100",
        "args": {"value": "int"},
    },
    "display.brightness.get": {"summary": "Read backlight brightness"},
    "display.preview": {"summary": "Render the composed canvas as a base64 PNG"},
    "layout.show": {"summary": "Show the active layout"},
    "layout.set": {"summary": "Replace the whole layout", "args": {"layout": "object"}},
    "layout.region.set": {
        "summary": "Add or edit a region",
        "args": {
            "name": "str",
            "x": "int?",
            "y": "int?",
            "w": "int?",
            "h": "int?",
            "instance": "str?",
        },
    },
    "layout.overlay.add": {
        "summary": "Add an overlay",
        "args": {"region": "str", "instance": "str"},
    },
    "layout.overlay.remove": {
        "summary": "Remove an overlay",
        "args": {"region": "str", "instance": "str"},
    },
    "plugin.list": {"summary": "List discovered plugins"},
    "plugin.describe": {"summary": "Describe one plugin manifest", "args": {"name": "str"}},
    "plugin.instance.list": {"summary": "List configured instances and their health"},
    "plugin.instance.add": {
        "summary": "Create a plugin instance",
        "args": {"name": "str", "plugin": "str", "settings": "object?"},
    },
    "plugin.instance.set": {
        "summary": "Change an instance's settings",
        "args": {"name": "str", "settings": "object"},
    },
    "plugin.instance.enable": {"summary": "Enable an instance", "args": {"name": "str"}},
    "plugin.instance.disable": {"summary": "Disable an instance", "args": {"name": "str"}},
    "plugin.instance.remove": {"summary": "Remove an instance", "args": {"name": "str"}},
    "plugin.call": {
        "summary": "Route a verb to a plugin instance",
        "args": {"instance": "str", "verb": "str", "args": "object?"},
    },
    "access.list": {"summary": "List the allowed users and group"},
    "access.allow_user": {"summary": "Add an allowed user", "args": {"name": "str"}},
    "access.deny_user": {"summary": "Remove an allowed user", "args": {"name": "str"}},
    "access.set_group": {"summary": "Set the allowed group", "args": {"name": "str"}},
    "access.clear_group": {"summary": "Clear the allowed group"},
    "logs.tail": {
        "summary": "Read the recent log ring",
        "args": {"lines": "int?", "level": "str?", "follow": "bool?"},
    },
    "schema.get": {"summary": "The machine-readable CLI surface"},
}


def make_dispatch(runner: Any) -> Callable[[protocol.Request], Any]:
    """Return the server-side ``dispatch(request)`` for ``runner``."""

    def dispatch(request: protocol.Request) -> Any:
        handler = HANDLERS.get(request.cmd)
        if handler is None:
            raise protocol.ProtocolError("invalid_input", f"unknown command {request.cmd!r}")
        try:
            result = handler(runner, dict(request.args or {}))
        except protocol.ProtocolError:
            raise
        except SettingError as exc:
            raise protocol.ProtocolError("invalid_input", exc.message, setting=exc.path) from exc
        except SettingsRejected as exc:
            raise protocol.ProtocolError(
                "invalid_input", exc.message, setting=exc.setting
            ) from exc
        except LayoutError as exc:
            # A layout rule violation is a validation failure (exit 2), not an
            # internal error: the message names the offending region (FR-006).
            raise protocol.ProtocolError("invalid_input", str(exc), setting="layout") from exc
        except SecretError as exc:
            raise protocol.ProtocolError("invalid_input", str(exc)) from exc
        except CommandError as exc:
            # ``CommandError`` carries the error code the plugin wants reported
            # (``contracts/plugin-sdk.md``: "raises CommandError(code, message) for
            # expected failures"), so honour it: a validation failure inside a
            # plugin's verb is ``invalid_input`` — exit 2, as
            # ``contracts/builtin-plugins.md`` requires — not ``plugin_error``.
            # Anything outside the documented set degrades to ``plugin_error``.
            code = exc.code if exc.code in protocol.ERROR_CODES else "plugin_error"
            raise protocol.ProtocolError(code, exc.message) from exc
        except SecretNotFound as exc:
            raise protocol.ProtocolError(
                "not_found", f"no secret named {exc.args[0]!r}"
            ) from exc
        except KeyError as exc:
            raise protocol.ProtocolError("not_found", f"missing argument {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - reported as a documented error code
            raise protocol.ProtocolError("internal", f"{request.cmd} failed: {exc}") from exc
        if isinstance(result, Iterator):
            return result
        return protocol.Response.success(request.id, result)

    return dispatch


# --------------------------------------------------------------------- runner


def _runner_status(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return runner.status()


def _runner_reload(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return runner.reload()


def _runner_stop(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    runner.request_stop(args.get("timeout_s"))
    return {"stopping": True}


# --------------------------------------------------------------------- config


def _config_get(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    path = _require(args, "path")
    return {"path": path, "value": _resolve(runner.config_data, path)}


def _config_show(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    data = runner.config_data
    path = args.get("path")
    if path:
        return {"path": path, "value": _resolve(data, path)}
    return {"config": data}


def _config_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    path = _require(args, "path")
    if "value" not in args:
        raise protocol.ProtocolError("invalid_input", "config.set needs a 'value'")
    if path == "plugins" or path.startswith("plugins."):
        # FR-006: validate before applying.  ``config set`` is the generic escape
        # hatch, but a plugins entry naming a plugin the loader never discovered
        # is exactly the "dead" instance ``plugin.instance.add`` rejects after
        # F5 (and ``contracts/cli.md`` lists "unknown plugin" at exit 2 for the
        # CLI as a whole).  Inspect the *resulting* subtree, not just the value,
        # so a dotted sub-key such as ``plugins.<name>.plugin`` cannot create one
        # either.  The write is refused before ``set_config`` touches anything.
        candidate = assign(runner.config_data, path, args["value"])
        _guard_plugin_instances(runner, candidate.get("plugins"))
    return runner.set_config(path, args["value"])


def _config_unset(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    path = _require(args, "path")
    return runner.unset_config(path)


def _config_validate(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    from smart_panel.core.config import validate_config  # local: avoid a config/runner cycle

    document = args.get("config", args)
    validated = validate_config(document)
    return {"valid": True, "version": validated.get("version")}


def _config_schema(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    from smart_panel.core.config import default_config

    core = {
        "display": _settings_schema(_DISPLAY_SETTINGS),
        "access": {"allowed_users": "list[str]", "allowed_group": "str | null"},
        "paths": {"spool": "path | null"},
        "defaults": default_config(),
    }
    plugins: dict[str, Any] = {}
    only = args.get("plugin")
    loader = runner.loader
    if loader is not None:
        for record in loader.records:
            if only and record.name != only:
                continue
            manifest = getattr(record.plugin_class, "manifest", None)
            if manifest is not None:
                plugins[record.name] = _manifest_schema(manifest)
    return {"core": core, "plugins": plugins}


# --------------------------------------------------------------------- secrets


def _secret_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    if "value" not in args:
        raise protocol.ProtocolError("invalid_input", "secret.set needs a 'value'")
    secrets = _secrets(runner)
    secrets.set(name, args["value"])
    runner.log.info("secret_set", name=name)
    return {"name": name, "stored": True}


def _secret_delete(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    _secrets(runner).delete(name)
    runner.log.info("secret_deleted", name=name)
    return {"name": name, "deleted": True}


def _secret_list(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return {"names": _secrets(runner).list_names()}


# --------------------------------------------------------------------- display


def _brightness_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    if "value" not in args:
        raise protocol.ProtocolError("invalid_input", "display.brightness.set needs a 'value'")
    return runner.set_brightness(args["value"])


def _brightness_get(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return {"value": runner.brightness()}


def _display_preview(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return {"png_b64": runner.preview_png()}


# --------------------------------------------------------------------- layout


def _layout_show(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return runner.config_data.get("layout", {"regions": [], "overlays": []})


def _layout_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    if "layout" not in args:
        raise protocol.ProtocolError("invalid_input", "layout.set needs a 'layout'")
    return runner.set_config("layout", args["layout"])


def _layout_region_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    regions = [dict(region) for region in runner.config_data["layout"]["regions"]]
    existing = next((region for region in regions if region["name"] == name), None)
    fields = {key: args[key] for key in ("x", "y", "w", "h", "instance") if key in args}
    if existing is None:
        missing = {"x", "y", "w", "h", "instance"} - set(fields)
        if missing:
            raise protocol.ProtocolError(
                "invalid_input", f"a new region {name!r} needs {sorted(missing)}"
            )
        regions.append({"name": name, **fields})
    else:
        existing.update(fields)
    return runner.set_config("layout.regions", regions)


def _layout_overlay_add(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    region = _require(args, "region")
    instance = _require(args, "instance")
    overlays = [dict(overlay) for overlay in runner.config_data["layout"]["overlays"]]
    if any(o["region"] == region and o["instance"] == instance for o in overlays):
        return {"region": region, "instance": instance, "added": False}
    overlays.append({"region": region, "instance": instance, "z": int(args.get("z", 0))})
    runner.set_config("layout.overlays", overlays)
    return {"region": region, "instance": instance, "added": True}


def _layout_overlay_remove(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    region = _require(args, "region")
    instance = _require(args, "instance")
    overlays = [
        dict(overlay)
        for overlay in runner.config_data["layout"]["overlays"]
        if not (overlay["region"] == region and overlay["instance"] == instance)
    ]
    runner.set_config("layout.overlays", overlays)
    return {"region": region, "instance": instance, "removed": True}


# -------------------------------------------------------------------- plugins


def _plugin_list(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    loader = runner.loader
    records = loader.records if loader is not None else []
    return {"plugins": [record.describe() for record in records]}


def _plugin_describe(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    loader = runner.loader
    record = loader.get(name) if loader is not None else None
    if record is None:
        raise protocol.ProtocolError("not_found", f"no plugin named {name!r}")
    manifest = getattr(record.plugin_class, "manifest", None)
    return {
        **record.describe(),
        "manifest": _manifest_schema(manifest) if manifest is not None else None,
    }


def _plugin_instance_list(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return {"instances": runner.plugin_instances()}


def _plugin_instance_add(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    plugin = _require(args, "plugin")
    # FR-006: validate before applying.  The criterion is the shared guard's, so
    # ``plugin.instance.add`` and ``config set`` on a ``plugins`` path agree on
    # the code, message and setting.  It runs before the settings are coerced,
    # so a name the loader never discovered is reported as ``invalid_input``
    # (exit 2) whatever else the caller sent.
    _guard_plugin_instances(runner, {name: {"plugin": plugin}})
    # FR-006: the settings must be an object.  The coercion accepts anything
    # ``dict(...)`` can consume (including a list of pairs); a value it cannot
    # (``["not-a-pair"]``, a bare string, an int) is an invalid change, not an
    # internal error, so it is reported as ``invalid_input`` (exit 2) naming the
    # setting -- the same class the other F3 validators use.
    raw_settings = args.get("settings", {}) or {}
    try:
        settings = dict(raw_settings)
    except (TypeError, ValueError) as exc:
        raise protocol.ProtocolError(
            "invalid_input",
            "plugin.instance.add needs a 'settings' object",
            setting=f"plugins.{name}.settings",
        ) from exc
    instance = {"plugin": plugin, "enabled": True, "settings": settings}
    return runner.set_config(f"plugins.{name}", instance)


def _plugin_instance_set(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    settings = args.get("settings")
    if not isinstance(settings, Mapping):
        raise protocol.ProtocolError(
            "invalid_input", "plugin.instance.set needs a 'settings' object"
        )
    current = runner.config_data["plugins"].get(name)
    if current is None:
        raise protocol.ProtocolError("not_found", f"no instance named {name!r}")
    merged = dict(current.get("settings", {}))
    merged.update(settings)
    return runner.set_config(f"plugins.{name}.settings", merged)


def _plugin_instance_enable(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    return runner.set_config(f"plugins.{name}.enabled", True)


def _plugin_instance_disable(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    return runner.set_config(f"plugins.{name}.enabled", False)


def _plugin_instance_remove(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    return runner.unset_config(f"plugins.{name}")


def _plugin_call(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    instance = _require(args, "instance")
    verb = _require(args, "verb")
    return runner.call_plugin(instance, verb, args.get("args", {}) or {})


# --------------------------------------------------------------------- access


def _access_list(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return runner.access_list()


def _access_allow_user(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    users = list(runner.config_data["access"]["allowed_users"])
    if name not in users:
        users.append(name)
        runner.set_config("access.allowed_users", users)
    return {"allowed_users": users}


def _access_deny_user(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    users = [user for user in runner.config_data["access"]["allowed_users"] if user != name]
    runner.set_config("access.allowed_users", users)
    return {"allowed_users": users}


def _access_set_group(runner: Any, args: dict[str, Any]) -> dict[str, Any]:
    name = _require(args, "name")
    runner.set_config("access.allowed_group", name)
    return {"allowed_group": name}


def _access_clear_group(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    runner.set_config("access.allowed_group", None)
    return {"allowed_group": None}


# ----------------------------------------------------------------------- logs


def _logs_tail(runner: Any, args: dict[str, Any]) -> Any:
    lines = int(args.get("lines", 50) or 50)
    level = args.get("level")
    events = [event for event in runner.log.recent() if _level_at_least(event, level)]
    if not args.get("follow"):
        return {"events": events[-lines:]}

    # Captured before the generator is pulled, so events that arrive while the
    # backlog is being delivered are not missed (the ring is a fixed-size deque,
    # so length-based indexing stops seeing new events once it is full).
    start_sequence = runner.log.sequence

    def stream() -> Iterator[dict[str, Any]]:
        for item in events[-lines:]:
            yield _log_event(item)
        sequence = start_sequence
        # Streams until the client closes; the server keeps the connection open
        # and writes each yielded line as it arrives (control-protocol.md).
        while not runner.stopping:
            time.sleep(0.2)
            for item in runner.log.events_since(sequence):
                if _level_at_least(item, level):
                    yield _log_event(item)
            sequence = runner.log.sequence

    return stream()


def _log_event(item: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap one log ring entry as the contract's ``{"event": "log", ...}`` line."""
    return {
        "event": "log",
        "log_event": item.get("event"),
        "ts": item.get("ts"),
        "level": item.get("level"),
        **{key: value for key, value in item.items() if key not in ("event", "ts", "level")},
    }


# --------------------------------------------------------------------- schema


def _schema_get(runner: Any, _args: dict[str, Any]) -> dict[str, Any]:
    return schema(runner)


def schema(runner: Any = None) -> dict[str, Any]:
    """The machine-readable CLI surface (``schema.get``/``smart-panel schema``)."""
    plugin_verbs: dict[str, Any] = {}
    if runner is not None and runner.loader is not None:
        for record in runner.loader.records:
            manifest = getattr(record.plugin_class, "manifest", None)
            if manifest is None:
                continue
            for command in manifest.commands:
                plugin_verbs[f"{manifest.name}.{command.name}"] = {
                    "summary": command.summary,
                    "spoolable": command.spoolable,
                    "args": [_setting_dict(setting) for setting in command.args],
                }
    return {
        "commands": _CORE_COMMANDS,
        "exit_codes": _EXIT_CODES,
        "plugin_verbs": plugin_verbs,
    }


HANDLERS: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "runner.status": _runner_status,
    "runner.reload": _runner_reload,
    "runner.stop": _runner_stop,
    "config.get": _config_get,
    "config.show": _config_show,
    "config.set": _config_set,
    "config.unset": _config_unset,
    "config.validate": _config_validate,
    "config.schema": _config_schema,
    "secret.set": _secret_set,
    "secret.delete": _secret_delete,
    "secret.list": _secret_list,
    "display.brightness.set": _brightness_set,
    "display.brightness.get": _brightness_get,
    "display.preview": _display_preview,
    "layout.show": _layout_show,
    "layout.set": _layout_set,
    "layout.region.set": _layout_region_set,
    "layout.overlay.add": _layout_overlay_add,
    "layout.overlay.remove": _layout_overlay_remove,
    "plugin.list": _plugin_list,
    "plugin.describe": _plugin_describe,
    "plugin.instance.list": _plugin_instance_list,
    "plugin.instance.add": _plugin_instance_add,
    "plugin.instance.set": _plugin_instance_set,
    "plugin.instance.enable": _plugin_instance_enable,
    "plugin.instance.disable": _plugin_instance_disable,
    "plugin.instance.remove": _plugin_instance_remove,
    "plugin.call": _plugin_call,
    "access.list": _access_list,
    "access.allow_user": _access_allow_user,
    "access.deny_user": _access_deny_user,
    "access.set_group": _access_set_group,
    "access.clear_group": _access_clear_group,
    "logs.tail": _logs_tail,
    "schema.get": _schema_get,
}


# -------------------------------------------------------------------- helpers

_DISPLAY_SETTINGS: tuple[Setting, ...] = (
    Setting(name="fps", type="int", default=15, min=5, max=30),
    Setting(name="brightness", type="int", default=50, min=0, max=100),
    Setting(name="min_text_px", type="int", default=24, min=12, max=64),
    Setting(name="sink", type="enum", default="hid", choices=["hid", "files", "null"]),
)

_LEVELS = ("debug", "info", "warning", "error")


def _require(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise protocol.ProtocolError("invalid_input", f"missing required string argument {key!r}")
    return value


def _secrets(runner: Any) -> Any:
    secrets = runner.secrets
    if secrets is None:
        raise protocol.ProtocolError("internal", "the runner has no secrets store")
    return secrets


def _resolve(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _guard_plugin_instances(runner: Any, instances: Any) -> None:
    """Reject plugin instances naming a plugin the loader never discovered.

    The criterion is exactly the one :meth:`Runner._start_instance` applies — a
    plugin absent from ``loader.loaded()`` — so no CLI config write can persist an
    instance the runner would only ever log ``instance_skipped`` for (FR-006).
    Both entry points that can persist an instance route through here:
    ``plugin.instance.add`` (a single instance it is about to create) and
    ``config set`` on a ``plugins`` path (the whole resulting subtree), so the
    two agree.  With no loader there is nothing to check against, so the write
    is allowed, matching ``plugin.instance.add``; a value that fails structural
    validation (a missing or non-string ``plugin``) is left to
    ``validate_config``.
    """
    loader = runner.loader
    if loader is None or not isinstance(instances, Mapping):
        return
    loaded = loader.loaded()
    for name, instance in instances.items():
        if not isinstance(instance, Mapping):
            continue
        plugin = instance.get("plugin")
        if plugin is not None and str(plugin) not in loaded:
            raise protocol.ProtocolError(
                "invalid_input",
                f"plugin {plugin!r} is not installed or available",
                setting=f"plugins.{name}.plugin",
            )


def _level_at_least(event: Mapping[str, Any], level: str | None) -> bool:
    if not level:
        return True
    try:
        threshold = _LEVELS.index(str(level))
    except ValueError:
        return True
    current = str(event.get("level", "info"))
    return _LEVELS.index(current) >= threshold if current in _LEVELS else True


def _setting_dict(setting: Setting) -> dict[str, Any]:
    data = asdict(setting)
    if isinstance(data.get("choices"), tuple):
        data["choices"] = list(data["choices"])
    if data.get("item") is not None:
        data["item"] = _setting_dict(setting.item)  # type: ignore[arg-type]
    return data


def _settings_schema(settings: tuple[Setting, ...]) -> dict[str, Any]:
    return {setting.name: _setting_dict(setting) for setting in settings}


def _manifest_schema(manifest: Any) -> dict[str, Any]:
    return {
        "name": manifest.name,
        "version": manifest.version,
        "summary": manifest.summary,
        "capabilities": sorted(manifest.capabilities),
        "settings": [_setting_dict(setting) for setting in manifest.settings],
        "commands": [
            {
                "name": command.name,
                "summary": command.summary,
                "spoolable": command.spoolable,
                "mutates_config": command.mutates_config,
                "args": [_setting_dict(setting) for setting in command.args],
            }
            for command in manifest.commands
        ],
        "source_types": [
            {"name": source.name, "metrics": list(source.metrics)}
            for source in manifest.source_types
        ],
    }
