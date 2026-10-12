"""The ``smart-panel`` CLI entry point (T054).

Global flags are ``--home``, ``--json``, ``--quiet``, ``--timeout`` and
``--refresh``; they come before the command. The subcommand tree is the static
core surface (T055-T060) plus one verb group per plugin manifest, generated from
the plugin loader and cached in ``<home>/cli-cache.json`` — the cache is rebuilt
when it is missing, when ``--refresh`` is given, when the local plugin set
changed, or when a reachable runner announces a different ``plugins_rev``. With
no runner and no cache the tree is still built from the loader, so spoolable
verbs (and their ``saved_for_later`` success) work on a cold start.

Every outcome is written through :class:`~smart_panel.cli.output.Output`, so an
argument error is the same JSON envelope and exit 2 as any other validation
failure (``contracts/cli.md``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from smart_panel.cli.commands import STREAMED, CliContext, parse_options
from smart_panel.cli.commands import config as config_commands
from smart_panel.cli.commands import display as display_commands
from smart_panel.cli.commands import misc as misc_commands
from smart_panel.cli.commands import plugins as plugin_commands
from smart_panel.cli.commands import runner as runner_commands
from smart_panel.cli.commands import service as service_commands
from smart_panel.cli.output import CliError, Output, UsageError

CACHE_VERSION = 1
DEFAULT_TIMEOUT_S = 5.0

# Plugin name -> CLI verb group (``contracts/cli.md`` names them for the
# built-ins; a third-party plugin keeps its own name).
PLUGIN_GROUP_ALIASES = {
    "alerts": "alert",
    "crawl": "crawl",
    "gauges": "gauge",
    "sidebar": "sidebar",
    "sources": "data",
}

COMMANDS: dict[tuple[str, ...], Any] = {}
for _module in (
    runner_commands,
    config_commands,
    display_commands,
    plugin_commands,
    misc_commands,
    service_commands,
):
    COMMANDS.update(_module.HANDLERS)

_USAGE = (
    "usage: smart-panel [--home DIR] [--json] [--quiet] [--timeout SECONDS]\n"
    "                   <command> [args]\n"
    "\n"
    "core: run start stop restart reload status logs service\n"
    "      config secret brightness layout preview plugin access schema version\n"
)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return the process exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_mode = os.environ.get("SMART_PANEL_JSON") == "1"
    quiet = False
    out = Output(json_mode=json_mode, quiet=quiet)

    try:
        home_arg, json_mode, quiet, timeout, refresh, rest = _parse_globals(arguments, json_mode)
    except CliError as exc:
        return out.fail(exc.code, exc.message, exc.setting)
    out = Output(json_mode=json_mode, quiet=quiet)

    if rest in (["--help"], ["-h"]):
        print(_USAGE, file=sys.stdout)
        return 0
    if not rest:
        return out.fail("invalid_input", "a command is required\n" + _USAGE)

    try:
        ctx = _build_context(home_arg, timeout, refresh, out)
        return _run(ctx, rest)
    except CliError as exc:
        return out.fail(exc.code, exc.message, exc.setting)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort envelope (exit 1)
        return out.fail("internal", f"{type(exc).__name__}: {exc}")


# ------------------------------------------------------------------- globals


def _parse_globals(
    argv: list[str], json_mode: bool
) -> tuple[str | None, bool, bool, float, bool, list[str]]:
    """Split the leading global flags from the command tokens."""
    home: str | None = None
    quiet = False
    timeout = DEFAULT_TIMEOUT_S
    refresh = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--json":
            json_mode = True
        elif token == "--quiet":
            quiet = True
        elif token == "--refresh":
            refresh = True
        elif token.startswith("--home="):
            home = token.split("=", 1)[1]
        elif token == "--home":
            index += 1
            if index >= len(argv):
                raise UsageError("--home needs a directory")
            home = argv[index]
        elif token.startswith("--timeout="):
            timeout = _timeout(token.split("=", 1)[1])
        elif token == "--timeout":
            index += 1
            if index >= len(argv):
                raise UsageError("--timeout needs a number of seconds")
            timeout = _timeout(argv[index])
        elif token.startswith("-") and token not in ("-h", "--help"):
            raise UsageError(f"unknown global option {token}")
        else:
            break
        index += 1
    return home, json_mode, quiet, timeout, refresh, argv[index:]


def _timeout(raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise UsageError(f"--timeout expected a number, got {raw!r}") from exc


def _build_context(
    home_arg: str | None, timeout: float, refresh: bool, out: Output
) -> CliContext:
    home = _resolve_home(home_arg)
    return CliContext(home=home, timeout=timeout, refresh=refresh, out=out)


def _resolve_home(home_arg: str | None) -> Path:
    if home_arg:
        os.environ["SMART_PANEL_HOME"] = str(home_arg)
    override = os.environ.get("SMART_PANEL_HOME")
    if override:
        return Path(override)
    return Path.home() / ".config" / "smart-panel"


# ------------------------------------------------------------------ dispatch


def _run(ctx: CliContext, tokens: list[str]) -> int:
    handler = _static_command(tokens)
    if handler is not None:
        path, function = handler
        data = function(ctx, tokens[len(path) :])
    else:
        data = _plugin_verb(ctx, tokens)
    if isinstance(data, type(STREAMED)) or data is STREAMED:
        return 0
    ctx.out.emit(data)
    return 0


def _static_command(tokens: list[str]) -> tuple[tuple[str, ...], Any] | None:
    for length in (3, 2, 1):
        if len(tokens) >= length:
            key = tuple(tokens[:length])
            if key in COMMANDS:
                return key, COMMANDS[key]
    return None


def _plugin_verb(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    groups = _plugin_groups(ctx)
    spec = _match_verb(groups, tokens)
    if spec is None:
        raise UsageError(f"unknown command {' '.join(tokens)!r}\n" + _USAGE)
    verb_spec, consumed = spec
    return _run_verb(ctx, verb_spec, tokens[consumed:])


def _match_verb(
    groups: dict[str, dict[str, dict[str, Any]]], tokens: list[str]
) -> tuple[dict[str, Any], int] | None:
    if not tokens:
        return None
    verbs = groups.get(tokens[0])
    if not verbs:
        return None
    rest = tokens[1:]
    for length in (3, 2, 1):
        if len(rest) >= length:
            key = " ".join(rest[:length])
            if key in verbs:
                return verbs[key], 1 + length
    raise UsageError(f"unknown {tokens[0]} verb: {' '.join(rest)!r}")


def _run_verb(ctx: CliContext, spec: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    declared = list(spec.get("args", []))
    value_opts = {arg["name"]: _arg_converter(arg) for arg in declared}
    positionals, opts = parse_options(tokens, value_opts=value_opts)
    if len(positionals) > len(declared):
        raise UsageError(f"too many arguments for {spec.get('verb')!r}")
    args: dict[str, Any] = {}
    for index, arg in enumerate(declared):
        name = arg["name"]
        key = name.replace("-", "_")
        if key in opts:
            args[name] = opts[key]
        elif index < len(positionals):
            args[name] = _coerce(arg, positionals[index])
    return ctx.call(
        "plugin.call",
        {"instance": spec["instance"], "verb": spec["verb"], "args": args},
        spoolable=bool(spec.get("spoolable")),
    )


def _arg_converter(arg: dict[str, Any]) -> Any:
    kind = arg.get("type")
    if kind == "int":
        return int
    if kind == "float":
        return float
    if kind in ("list", "object"):
        from smart_panel.cli.commands import json_or_str

        return json_or_str
    return str


def _coerce(arg: dict[str, Any], raw: str) -> Any:
    from smart_panel.cli.commands import json_or_str

    kind = arg.get("type")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind in ("list", "object"):
        return json_or_str(raw)
    return raw


# -------------------------------------------------------------- plugin tree


def _plugin_groups(ctx: CliContext) -> dict[str, dict[str, dict[str, Any]]]:
    """The plugin verb groups, from the cache when fresh, else the loader."""
    cache_path = ctx.home / "cli-cache.json"
    cached = _read_cache(cache_path)
    loader = ctx.plugin_loader()
    local_rev = loader.plugins_rev
    server_rev = _server_plugins_rev(ctx)

    fresh = (
        cached is not None
        and cached.get("v") == CACHE_VERSION
        and cached.get("plugins_rev") == local_rev
        and (server_rev is None or server_rev == local_rev)
    )
    if not ctx.refresh and fresh:
        groups = cached.get("groups")
        if isinstance(groups, dict):
            return groups

    groups = _build_groups(ctx, loader)
    _write_cache(cache_path, local_rev, groups)
    return groups


def _build_groups(
    ctx: CliContext, loader: Any
) -> dict[str, dict[str, dict[str, Any]]]:
    from smart_panel.core.plugins import LoadState

    instances = _configured_instances(ctx)
    groups: dict[str, dict[str, dict[str, Any]]] = {}
    for record in loader.records:
        if record.state != LoadState.LOADED or record.plugin_class is None:
            continue
        manifest = getattr(record.plugin_class, "manifest", None)
        if manifest is None:
            continue
        group = PLUGIN_GROUP_ALIASES.get(manifest.name, manifest.name)
        instance = _default_instance(manifest.name, instances)
        verbs: dict[str, dict[str, Any]] = {}
        for command in manifest.commands:
            verbs[command.name.replace("-", " ")] = {
                "instance": instance,
                "verb": command.name,
                "spoolable": bool(command.spoolable),
                "summary": command.summary,
                "args": [_arg_dict(setting) for setting in command.args],
            }
        if verbs:
            groups[group] = verbs
    return groups


def _configured_instances(ctx: CliContext) -> dict[str, str]:
    """Instance name -> plugin name, from the local config (best effort)."""
    path = ctx.home / "config.json"
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    plugins = document.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    return {
        str(name): str(entry.get("plugin"))
        for name, entry in plugins.items()
        if isinstance(entry, dict) and entry.get("plugin")
    }


def _default_instance(plugin: str, instances: dict[str, str]) -> str:
    candidates = sorted(name for name, owner in instances.items() if owner == plugin)
    if not candidates or plugin in candidates:
        return plugin
    return candidates[0]


def _arg_dict(setting: Any) -> dict[str, Any]:
    return {
        "name": setting.name,
        "type": setting.type,
        "default": setting.default,
        "choices": list(setting.choices) if setting.choices else None,
    }


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_cache(path: Path, rev: int, groups: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps({"v": CACHE_VERSION, "plugins_rev": rev, "groups": groups}, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:  # pragma: no cover - a cache we cannot write is not fatal
        pass


def _server_plugins_rev(ctx: CliContext) -> int | None:
    """A reachable runner's ``plugins_rev``, or ``None`` when there is none."""
    from smart_panel.core import paths
    from smart_panel.core.control.client import ControlClient

    socket_path = paths.control_sock()
    if not socket_path.exists():
        return None
    control = ControlClient(timeout=min(ctx.timeout, 1.0))
    try:
        control.connect()
        hello = control.server_hello
        return hello.plugins_rev if hello is not None else None
    except Exception:  # noqa: BLE001 - a cache hint must never fail a command
        return None
    finally:
        control.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
