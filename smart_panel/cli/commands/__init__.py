"""Shared plumbing for the CLI command modules (T053-T060).

Every command handler is ``handler(ctx, tokens) -> data`` where ``tokens`` are
the words after the command path. A handler returns a JSON-compatible mapping
(printed through the envelope) or raises :class:`~smart_panel.cli.output.CliError`
with the documented code. :class:`CliContext` carries the resolved home, the
control timeout and the output object, and owns the two control-channel
transports the CLI uses:

* :meth:`CliContext.call` — one request/response over ``ControlClient``, with the
  client's own exit-3/5/6 conditions mapped onto :class:`CliError`;
* :meth:`CliContext.try_control` — the same, returning ``None`` when no runner is
  running so a command that can work from the config file directly falls back
  (``config show|get|set|unset|validate|schema|path``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smart_panel.cli.output import CliError, Output, UsageError
from smart_panel.core.control import client
from smart_panel.core.control.client import ControlClient

Converter = Callable[[str], Any]


class Streamed:
    """Sentinel: a handler already wrote its output (``logs --follow``)."""


STREAMED = Streamed()


def json_or_str(raw: str) -> Any:
    """Parse ``raw`` as JSON, falling back to the raw string (``config set`` rules)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _convert(converter: Converter, raw: str, name: str) -> Any:
    try:
        return converter(raw)
    except (TypeError, ValueError) as exc:
        label = getattr(converter, "__name__", "a value")
        raise UsageError(f"--{name} expected {label}") from exc


def parse_options(
    tokens: Sequence[str],
    *,
    value_opts: Mapping[str, Converter] | None = None,
    bool_opts: Sequence[str] = (),
    repeat_opts: Sequence[str] = (),
) -> tuple[list[str], dict[str, Any]]:
    """Split ``tokens`` into positionals and options.

    Args:
        value_opts: ``--name`` options that take one value, mapped to a converter.
        bool_opts: ``--name`` flags that take no value.
        repeat_opts: ``--name`` options that may repeat; values are collected
            into a list (for example ``--set K=V --set K2=V2``).

    Returns:
        ``(positionals, options)`` where option keys are the hyphen-free names.
    """
    values = dict(value_opts or {})
    booleans = set(bool_opts)
    repeats = set(repeat_opts)
    positionals: list[str] = []
    options: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positionals.extend(tokens[index + 1 :])
            break
        if token.startswith("--") and len(token) > 2:
            name, has_eq, raw = token[2:].partition("=")
            key = name.replace("-", "_")
            if name in booleans:
                if has_eq:
                    raise UsageError(f"--{name} takes no value")
                options[key] = True
            elif name in values:
                if has_eq:
                    value = raw
                else:
                    index += 1
                    if index >= len(tokens):
                        raise UsageError(f"--{name} needs a value")
                    value = tokens[index]
                options[key] = _convert(values[name], value, name)
            elif name in repeats:
                if not has_eq:
                    index += 1
                    if index >= len(tokens):
                        raise UsageError(f"--{name} needs a value")
                    raw = tokens[index]
                options.setdefault(key, []).append(raw)
            else:
                raise UsageError(f"unknown option --{name}")
        elif token.startswith("-") and token != "-":
            raise UsageError(f"unknown option {token}")
        else:
            positionals.append(token)
        index += 1
    return positionals, options


def parse_pairs(tokens: Sequence[str]) -> dict[str, Any]:
    """Parse ``KEY=VALUE`` words into a dict (values JSON-or-string)."""
    pairs: dict[str, Any] = {}
    for token in tokens:
        key, has_eq, raw = token.partition("=")
        if not has_eq or not key:
            raise UsageError(f"expected KEY=VALUE, got {token!r}")
        pairs[key] = json_or_str(raw)
    return pairs


def require_args(positionals: Sequence[str], count: int, usage: str) -> list[str]:
    """Return exactly ``count`` positionals or raise a usage error."""
    if len(positionals) < count:
        raise UsageError(f"usage: {usage}")
    if len(positionals) > count:
        raise UsageError(f"too many arguments for {usage}")
    return list(positionals)


def no_args(positionals: Sequence[str], usage: str) -> None:
    """Raise a usage error when a command was given positionals."""
    if positionals:
        raise UsageError(f"usage: {usage}")


@dataclass
class CliContext:
    """Everything a command handler needs.

    Args:
        home: The resolved ``<home>`` (``--home``, ``$SMART_PANEL_HOME`` or the
            default ``~/.config/smart-panel``).
        timeout: Control-channel timeout in seconds.
        refresh: ``--refresh`` — rebuild the cached plugin command tree.
        out: The output object (``--json``/``--quiet`` aware).
    """

    home: Path
    timeout: float
    refresh: bool
    out: Output

    # ------------------------------------------------------------- transport

    def call(
        self,
        cmd: str,
        args: Mapping[str, Any] | None = None,
        *,
        spoolable: bool = False,
    ) -> dict[str, Any]:
        """Send one control command and return its ``data`` (or raise)."""
        control = ControlClient(timeout=self.timeout)
        try:
            try:
                response = control.call(cmd, dict(args or {}), spoolable=spoolable)
            except client.SpoolFull as exc:
                raise CliError(str(exc), code="spool_full") from exc
            except client.ControlClientError as exc:
                raise CliError(exc.message, code=exc.code) from exc
        finally:
            control.close()
        if not response.ok:
            error = response.error
            raise CliError(
                error.message if error is not None else "the runner reported a failure",
                code=error.code if error is not None else "internal",
                setting=error.setting if error is not None else None,
            )
        return response.data or {}

    def try_control(
        self, cmd: str, args: Mapping[str, Any] | None = None, *, spoolable: bool = False
    ) -> dict[str, Any] | None:
        """Like :meth:`call`, but ``None`` when no runner is running."""
        try:
            return self.call(cmd, args, spoolable=spoolable)
        except CliError as exc:
            if exc.code == "runner_not_running":
                return None
            raise

    def stream(self, cmd: str, args: Mapping[str, Any] | None = None) -> Any:
        """Open a streaming control command and yield each event object.

        Client-side failures raise the same :class:`CliError` codes as
        :meth:`call` (``runner_not_running`` for a follow with no runner,
        ``not_permitted`` for a rejected owner), so a streaming command exits 3/5
        exactly like its one-shot sibling.
        """
        control = ControlClient(timeout=self.timeout)
        try:
            try:
                yield from control.stream(cmd, dict(args or {}))
            except client.ControlClientError as exc:
                raise CliError(exc.message, code=exc.code) from exc
        finally:
            control.close()

    # ----------------------------------------------------------- local files

    def owns_home(self) -> bool:
        """Whether this account owns ``<home>`` (so it may edit the config directly)."""
        try:
            return self.home.stat().st_uid == os.geteuid()
        except OSError:
            return False

    def local_config(self) -> Any:
        """A :class:`~smart_panel.core.config.Config` for a runner-down fallback.

        Raises:
            CliError: ``runner_not_running`` when the config file is not ours.
        """
        if not self.owns_home():
            raise CliError(
                "the runner is not running and this account does not own the config file",
                code="runner_not_running",
            )
        from smart_panel.core.config import Config

        return Config(path=self.home / "config.json")

    def plugin_loader(self) -> Any:
        """A :class:`~smart_panel.core.plugins.PluginLoader` for this host."""
        from smart_panel.core.plugins import PluginLoader

        return PluginLoader()


def stdin_value() -> str:
    """Read a value from stdin (for ``secret set --stdin``)."""
    import sys

    return sys.stdin.read().rstrip("\n")
