"""Plugin management commands (T058).

``plugin list|describe|instance ...|call``. Instance settings are given as
``--set K=V`` / ``K=V`` pairs whose values are parsed as JSON when possible, and
are validated against the manifest by the runner (``plugin.instance.add`` /
``config set``), so an unknown key is refused with the plugin's own message.
"""

from __future__ import annotations

import json
from typing import Any

from smart_panel.cli.commands import (
    CliContext,
    no_args,
    parse_options,
    parse_pairs,
    require_args,
)
from smart_panel.cli.output import CliError, UsageError

HANDLERS: dict[tuple[str, ...], Any] = {}


def cmd_plugin_list(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """List discovered plugins (built-in, entry point and drop-in)."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel plugin list")
    return ctx.call("plugin.list")


def cmd_plugin_describe(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Describe one plugin's manifest."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel plugin describe NAME")
    return ctx.call("plugin.describe", {"name": name})


def cmd_plugin_instance_list(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """List configured instances and their health."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel plugin instance list")
    return ctx.call("plugin.instance.list")


def cmd_plugin_instance_add(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Create a plugin instance (``--plugin`` and optional ``--set K=V``)."""
    positionals, opts = parse_options(
        tokens, value_opts={"plugin": str}, repeat_opts=("set",)
    )
    (name,) = require_args(
        positionals, 1, "smart-panel plugin instance add NAME --plugin P [--set K=V ...]"
    )
    plugin = opts.get("plugin")
    if not plugin:
        raise UsageError("plugin instance add needs --plugin P")
    settings = parse_pairs(opts.get("set", []))
    return ctx.call(
        "plugin.instance.add", {"name": name, "plugin": plugin, "settings": settings}
    )


def cmd_plugin_instance_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Change an instance's settings (``NAME K=V [K=V ...]``)."""
    positionals, _opts = parse_options(tokens)
    if len(positionals) < 2:
        raise UsageError("usage: smart-panel plugin instance set NAME K=V [K=V ...]")
    name, *pairs = positionals
    return ctx.call("plugin.instance.set", {"name": name, "settings": parse_pairs(pairs)})


def cmd_plugin_instance_enable(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Enable an instance."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel plugin instance enable NAME")
    return ctx.call("plugin.instance.enable", {"name": name})


def cmd_plugin_instance_disable(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Disable an instance."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel plugin instance disable NAME")
    return ctx.call("plugin.instance.disable", {"name": name})


def cmd_plugin_instance_remove(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Remove an instance."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel plugin instance remove NAME")
    return ctx.call("plugin.instance.remove", {"name": name})


def cmd_plugin_call(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Route a verb to any plugin instance."""
    positionals, opts = parse_options(
        tokens, value_opts={"json-args": str}, repeat_opts=("arg",)
    )
    instance, verb = require_args(
        positionals, 2, "smart-panel plugin call INSTANCE VERB [--arg K=V ...]"
    )
    args = parse_pairs(opts.get("arg", []))
    if "json_args" in opts:
        args.update(_json_object(opts["json_args"]))
    return ctx.call("plugin.call", {"instance": instance, "verb": verb, "args": args})


def _json_object(raw: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"--json-args is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise CliError("--json-args must be a JSON object", code="invalid_input")
    return document


HANDLERS.update(
    {
        ("plugin", "list"): cmd_plugin_list,
        ("plugin", "describe"): cmd_plugin_describe,
        ("plugin", "instance", "list"): cmd_plugin_instance_list,
        ("plugin", "instance", "add"): cmd_plugin_instance_add,
        ("plugin", "instance", "set"): cmd_plugin_instance_set,
        ("plugin", "instance", "enable"): cmd_plugin_instance_enable,
        ("plugin", "instance", "disable"): cmd_plugin_instance_disable,
        ("plugin", "instance", "remove"): cmd_plugin_instance_remove,
        ("plugin", "call"): cmd_plugin_call,
    }
)
