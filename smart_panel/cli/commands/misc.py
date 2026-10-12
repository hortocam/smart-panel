"""Cross-cutting commands: access, logs, schema, version (T059).

``access`` manages the allow-list (runner account or root only, enforced by the
server). ``logs`` tails the runner's in-memory ring, optionally following it:
``--follow`` streams ``{"event": "log", ...}`` lines until the client closes
(``contracts/control-protocol.md``). ``schema`` is one machine-readable document
describing the whole surface, and ``version`` needs no permission.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from smart_panel.cli.commands import (
    STREAMED,
    CliContext,
    no_args,
    parse_options,
    require_args,
)

HANDLERS: dict[tuple[str, ...], Any] = {}


# ------------------------------------------------------------------- access


def cmd_access_list(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """List the allowed users and group."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel access list")
    return ctx.call("access.list")


def cmd_access_allow_user(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Add an allowed user."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel access allow-user NAME")
    return ctx.call("access.allow_user", {"name": name})


def cmd_access_deny_user(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Remove an allowed user."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel access deny-user NAME")
    return ctx.call("access.deny_user", {"name": name})


def cmd_access_set_group(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Set the allowed group."""
    positionals, _opts = parse_options(tokens)
    (name,) = require_args(positionals, 1, "smart-panel access set-group NAME")
    return ctx.call("access.set_group", {"name": name})


def cmd_access_clear_group(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Clear the allowed group."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel access clear-group")
    return ctx.call("access.clear_group")


# --------------------------------------------------------------------- logs


def cmd_logs(ctx: CliContext, tokens: list[str]) -> Any:
    """Tail the runner log, optionally following it."""
    positionals, opts = parse_options(
        tokens, value_opts={"lines": int, "level": str}, bool_opts=("follow",)
    )
    no_args(positionals, "smart-panel logs [--follow] [--lines N] [--level L]")
    args: dict[str, Any] = {"lines": opts.get("lines", 50)}
    if "level" in opts:
        args["level"] = opts["level"]
    if opts.get("follow"):
        return _follow(ctx, args)
    return ctx.call("logs.tail", args)


def _follow(ctx: CliContext, args: dict[str, Any]) -> Any:
    for event in ctx.stream("logs.tail", {**args, "follow": True}):
        ctx.out.emit_line(event)
    return STREAMED


# ------------------------------------------------------------------- schema


def cmd_schema(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """One JSON document describing every command, argument and exit code."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel schema")
    data = ctx.try_control("schema.get")
    if data is None:
        data = _local_schema(ctx)
    return data


def cmd_version(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Package and protocol versions (no permission required)."""
    from smart_panel import __version__
    from smart_panel.core.control.protocol import PROTOCOL_VERSION

    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel version")
    return {"version": __version__, "protocol": PROTOCOL_VERSION}


def _local_schema(ctx: CliContext) -> dict[str, Any]:
    # The runner serves the same payload (T052's ``schema.get``); when it is
    # down the CLI rebuilds it from the core helper plus the local loader.
    from smart_panel.core import commands as core_commands

    return core_commands.schema(SimpleNamespace(loader=ctx.plugin_loader()))


HANDLERS.update(
    {
        ("access", "list"): cmd_access_list,
        ("access", "allow-user"): cmd_access_allow_user,
        ("access", "deny-user"): cmd_access_deny_user,
        ("access", "set-group"): cmd_access_set_group,
        ("access", "clear-group"): cmd_access_clear_group,
        ("logs",): cmd_logs,
        ("schema",): cmd_schema,
        ("version",): cmd_version,
    }
)
