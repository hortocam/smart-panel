"""The CLI's output envelope, human formatter and exit codes (T053).

Every command answers in one of two shapes (``contracts/cli.md``):

* success — ``{"ok": true, "data": {...}}`` on stdout;
* failure — ``{"ok": false, "error": {"code", "message", "setting"?}}``.

In ``--json`` mode failures are written to **stdout** too, so an agent parses a
single stream; in human mode the message goes to stderr and the exit code is the
machine-readable signal. ``--quiet`` suppresses human output entirely (exit code
only).

The error-code-to-exit-code table is the one :mod:`smart_panel.core.control.protocol`
already owns (T044) — this module only re-exports it, so the CLI and the control
protocol can never disagree about what exit 2 means.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from smart_panel.core.control import protocol

# Re-exported so callers/tests pin the CLI to the protocol's single table.
EXIT_CODES: dict[str, int] = dict(protocol.EXIT_CODES)
EXIT_CODE_NAMES: dict[int, str] = dict(protocol.EXIT_CODE_NAMES)


def exit_code_for(code: str) -> int:
    """The exit code for an error code (an unknown code is an internal error)."""
    return protocol.exit_code_for(code)


class CliError(Exception):
    """A CLI failure; its ``code`` maps to a documented exit code."""

    code = "internal"

    def __init__(
        self, message: str, *, code: str | None = None, setting: str | None = None
    ) -> None:
        self.message = message
        self.setting = setting
        if code is not None:
            self.code = code
        super().__init__(message)


class UsageError(CliError):
    """An argument/usage error (exit 2)."""

    code = "invalid_input"


class ConflictError(CliError):
    """A resource is already in use, for example a second runner (exit 6)."""

    code = "conflict"


def success_envelope(data: Any = None) -> dict[str, Any]:
    """The success envelope: ``{"ok": true, "data": ...}``."""
    return {"ok": True, "data": data if data is not None else {}}


def failure_envelope(
    code: str, message: str, setting: str | None = None
) -> dict[str, Any]:
    """The failure envelope: ``{"ok": false, "error": {...}}``."""
    error: dict[str, Any] = {"code": code, "message": message}
    if setting is not None:
        error["setting"] = setting
    return {"ok": False, "error": error}


def dumps(payload: Any) -> str:
    """Serialise one payload as a single line of JSON."""
    return json.dumps(payload, separators=(",", ":"), default=str, sort_keys=False)


def format_human(data: Any) -> str:
    """Render a result dict as ``key: value`` lines (nested values as JSON)."""
    if data is None:
        return ""
    if isinstance(data, dict):
        lines = [f"{key}: {_scalar(value)}" for key, value in data.items()]
        return "\n".join(lines)
    if isinstance(data, (list, tuple)):
        return "\n".join(_scalar(item) for item in data)
    return _scalar(data)


def _scalar(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Output:
    """Writes success/failure output honoring ``--json`` and ``--quiet``.

    Args:
        json_mode: Emit the JSON envelope on stdout.
        quiet: Suppress human-readable output (exit code only).
        stdout: Stream for machine output (defaults to ``sys.stdout``).
        stderr: Stream for human error text (defaults to ``sys.stderr``).
    """

    def __init__(
        self,
        *,
        json_mode: bool = False,
        quiet: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    def emit(self, data: Any = None) -> None:
        """Write a successful result."""
        if self.json_mode:
            print(dumps(success_envelope(data)), file=self.stdout)
        elif not self.quiet:
            text = format_human(data)
            if text:
                print(text, file=self.stdout)

    def emit_line(self, payload: Any) -> None:
        """Write one streaming event (``logs --follow``) as its own line."""
        if self.json_mode:
            print(dumps(payload), file=self.stdout)
        elif not self.quiet:
            print(format_human(payload), file=self.stdout)

    def fail(self, code: str, message: str, setting: str | None = None) -> int:
        """Write a failure and return its exit code."""
        if self.json_mode:
            print(dumps(failure_envelope(code, message, setting)), file=self.stdout)
        elif not self.quiet:
            print(f"smart-panel: {message}", file=self.stderr)
        return exit_code_for(code)
