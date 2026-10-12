"""Configuration and secret commands (T056).

``config show|get|set|unset|validate|schema|path`` and ``secret set|list|delete``.
Configuration commands talk to the runner when one is running; when it is not,
and this account owns ``<home>``, they read and write ``config.json`` directly
(validating the whole candidate first, exactly as the runner does), so an
operator can fix a config before the runner will start. Secrets always need the
runner: only it holds the resolved secret store, and values are never echoed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from smart_panel.cli.commands import (
    CliContext,
    no_args,
    parse_options,
    require_args,
    stdin_value,
)
from smart_panel.cli.output import CliError, UsageError

HANDLERS: dict[tuple[str, str], Any] = {}


def _resolve(data: Any, dotted: str) -> Any:
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _invalid(exc: Exception) -> CliError:
    setting = getattr(exc, "path", None)
    return CliError(str(getattr(exc, "message", exc)), code="invalid_input", setting=setting)


# ------------------------------------------------------------------- config


def cmd_config_show(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Print the config or one dotted path."""
    positionals, _opts = parse_options(tokens)
    if len(positionals) > 1:
        raise UsageError("usage: smart-panel config show [PATH]")
    path = positionals[0] if positionals else None
    data = ctx.try_control("config.show", {"path": path} if path else {})
    if data is None:
        config = ctx.local_config().load()
        if path:
            return {"path": path, "value": _resolve(config, path)}
        return {"config": config}
    return data


def cmd_config_get(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Print one value by dotted path."""
    positionals, _opts = parse_options(tokens)
    (path,) = require_args(positionals, 1, "smart-panel config get PATH")
    data = ctx.try_control("config.get", {"path": path})
    if data is None:
        config = ctx.local_config().load()
        return {"path": path, "value": _resolve(config, path)}
    return data


def cmd_config_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Set a dotted path to a value (parsed as JSON, else a string).

    A successful result carries ``applied: true``: the runner only answers after
    the whole candidate validated, was applied live **and** written to disk, so a
    success means the change is durable. (A hard kill between the live apply and
    the save is inherent to the apply-then-persist order and persists nothing for
    that change; see the wave-3 carry-over note.)
    """
    from smart_panel.cli.commands import json_or_str

    positionals, _opts = parse_options(tokens)
    path, raw = require_args(positionals, 2, "smart-panel config set PATH VALUE")
    value = json_or_str(raw)
    data = ctx.try_control("config.set", {"path": path, "value": value})
    if data is None:
        return _local_write(ctx, path, value, unset=False)
    return data


def cmd_config_unset(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Reset a dotted path to its schema default."""
    positionals, _opts = parse_options(tokens)
    (path,) = require_args(positionals, 1, "smart-panel config unset PATH")
    data = ctx.try_control("config.unset", {"path": path})
    if data is None:
        return _local_write(ctx, path, None, unset=True)
    return data


def cmd_config_path(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Print the config file location."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel config path")
    return {"path": str(ctx.home / "config.json")}


def cmd_config_validate(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Validate a config document without applying it."""
    from smart_panel.core.config import migrate, validate_config
    from smart_panel.core.layout import LayoutError
    from smart_panel.sdk.settings import SettingError

    positionals, opts = parse_options(tokens, value_opts={"file": str})
    no_args(positionals, "smart-panel config validate [--file F]")
    target = Path(opts["file"]) if "file" in opts else (ctx.home / "config.json")
    if not target.exists():
        raise CliError(f"{target} does not exist", code="invalid_input")
    try:
        raw = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"{target} is not valid JSON: {exc}", code="invalid_input") from exc
    try:
        validated = validate_config(migrate(raw))
    except (SettingError, LayoutError) as exc:
        raise _invalid(exc) from exc
    return {"valid": True, "version": validated.get("version")}


def cmd_config_schema(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Emit the JSON schema of the core config and plugin settings."""
    positionals, opts = parse_options(tokens, value_opts={"plugin": str})
    no_args(positionals, "smart-panel config schema [--plugin NAME]")
    args: dict[str, Any] = {}
    if "plugin" in opts:
        args["plugin"] = opts["plugin"]
    data = ctx.try_control("config.schema", args)
    if data is None:
        data = _local_config_schema(ctx, args.get("plugin"))
    return data


# ------------------------------------------------------------------- secrets


def cmd_secret_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Store a secret (never echoed)."""
    positionals, opts = parse_options(tokens, bool_opts=("stdin",))
    if not positionals:
        raise UsageError("usage: smart-panel secret set NAME [--stdin]")
    if len(positionals) > 2:
        raise UsageError("usage: smart-panel secret set NAME [--stdin]")
    name = positionals[0]
    if opts.get("stdin"):
        value = stdin_value()
    elif len(positionals) == 2:
        value = positionals[1]
    else:
        raise UsageError("secret set needs a VALUE or --stdin")
    ctx.call("secret.set", {"name": name, "value": value})
    return {"name": name, "stored": True}


def cmd_secret_list(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """List secret names only."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel secret list")
    return ctx.call("secret.list")


def cmd_secret_delete(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Delete a secret by name."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel secret delete NAME")
    ctx.call("secret.delete", {"name": name})
    return {"name": name, "deleted": True}


# ---------------------------------------------------------------- internals


def _local_write(ctx: CliContext, path: str, value: Any, *, unset: bool) -> dict[str, Any]:
    from smart_panel.core.layout import LayoutError
    from smart_panel.sdk.settings import SettingError

    config = ctx.local_config()
    try:
        config.load()
        data = config.unset(path) if unset else config.set(path, value)
        config.save(data)
    except (SettingError, LayoutError) as exc:
        raise _invalid(exc) from exc
    except OSError as exc:
        raise CliError(f"could not write the config: {exc}") from exc
    if unset:
        return {"path": path, "unset": True, "applied": True}
    return {"path": path, "value": config.get(path), "applied": True}


def _local_config_schema(ctx: CliContext, plugin: str | None) -> dict[str, Any]:
    # The runner serves exactly this payload (T052's ``config.schema``); when it
    # is down the CLI rebuilds it from the same core helpers plus the local
    # plugin loader, so the two never diverge.
    from smart_panel.core import commands as core_commands

    stub = SimpleNamespace(loader=ctx.plugin_loader())
    args: dict[str, Any] = {"plugin": plugin} if plugin else {}
    return core_commands._config_schema(stub, args)


HANDLERS.update(
    {
        ("config", "show"): cmd_config_show,
        ("config", "get"): cmd_config_get,
        ("config", "set"): cmd_config_set,
        ("config", "unset"): cmd_config_unset,
        ("config", "validate"): cmd_config_validate,
        ("config", "schema"): cmd_config_schema,
        ("config", "path"): cmd_config_path,
        ("secret", "set"): cmd_secret_set,
        ("secret", "list"): cmd_secret_list,
        ("secret", "delete"): cmd_secret_delete,
    }
)
