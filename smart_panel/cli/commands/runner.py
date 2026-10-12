"""Runner lifecycle commands: run, start, stop, restart, reload, status (T055).

``run`` is the foreground entry point the service unit and ``start`` both use.
``start`` detaches a ``run`` with ``start_new_session=True`` and waits for the
socket and the first frame (or the timeout), exiting 6 when a runner is already
running. Every other command is one control request.

The test-only ``--clock-file`` (honored only under ``SMART_PANEL_TESTING=1``,
``runner.py``) is threaded through both ``run`` and ``start`` so a subprocess
integration test can advance a fake clock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from smart_panel.cli.commands import (
    CliContext,
    no_args,
    parse_options,
)
from smart_panel.cli.output import CliError, ConflictError, UsageError
from smart_panel.core import paths

DEFAULT_START_TIMEOUT_S = 10.0
_SINK_CHOICES = ("hid", "files", "null")
_START_OPTS = {"sink": str, "files-dir": str, "clock-file": str}


def _validate_sink(value: str) -> str:
    if value not in _SINK_CHOICES:
        raise UsageError(f"--sink must be one of {', '.join(_SINK_CHOICES)}")
    return value


def cmd_run(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Run the runner in the foreground (used by the service)."""
    positionals, opts = parse_options(
        tokens,
        value_opts={**_START_OPTS, "sink": _validate_sink},
    )
    no_args(positionals, "smart-panel run [--sink hid|files|null] [--files-dir DIR]")
    return _run_foreground(ctx, opts)


def cmd_start(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Start the runner in the background and wait for the first frame.

    The wait is bounded by the global ``--timeout`` (``max(ctx.timeout,
    DEFAULT_START_TIMEOUT_S)``); there is no per-command ``--timeout`` — the
    contract names ``--timeout`` as a global flag only.
    """
    positionals, opts = parse_options(
        tokens,
        value_opts={**_START_OPTS, "sink": _validate_sink},
    )
    no_args(positionals, "smart-panel start [--sink hid|files|null] [--files-dir DIR]")
    return _start_detached(ctx, opts, max(ctx.timeout, DEFAULT_START_TIMEOUT_S))


def cmd_stop(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Stop the runner and wait for it to release the socket."""
    positionals, opts = parse_options(tokens, value_opts={"timeout": float})
    no_args(positionals, "smart-panel stop [--timeout S]")
    args: dict[str, Any] = {}
    if "timeout" in opts:
        args["timeout_s"] = opts["timeout"]
    ctx.call("runner.stop", args)
    _wait_for_stop(ctx, float(opts.get("timeout", max(ctx.timeout, 10.0))))
    return {"stopping": True, "state": "stopped"}


def cmd_restart(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Stop then start the runner, waiting for content again."""
    positionals, opts = parse_options(
        tokens, value_opts={**_START_OPTS, "sink": _validate_sink}
    )
    no_args(positionals, "smart-panel restart")
    if _is_running(ctx):
        ctx.call("runner.stop")
        _wait_for_stop(ctx, max(ctx.timeout, 10.0))
    return _start_detached(ctx, opts, max(ctx.timeout, DEFAULT_START_TIMEOUT_S))


def cmd_reload(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Re-read ``config.json`` from disk and apply it live."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel reload")
    return ctx.call("runner.reload")


def cmd_status(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Report the runner state, frames, panel, layout, plugins and spool depth."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel status")
    return ctx.call("runner.status")


# ---------------------------------------------------------------- internals


def _run_foreground(ctx: CliContext, opts: dict[str, Any]) -> dict[str, Any]:
    from smart_panel.core.config import Config, assign, validate_config
    from smart_panel.core.paths import RuntimeOwnershipError
    from smart_panel.core.runner import Runner, RunnerConflict

    config = Config(path=ctx.home / "config.json")
    data = config.load()
    if "sink" in opts:
        data = validate_config(assign(data, "display.sink", opts["sink"]))
    if "files_dir" in opts:
        data = validate_config(assign(data, "display.files_dir", opts["files_dir"]))
    # Persist the startup overrides so a later restart/reload keeps the sink.
    try:
        config.save(data)
    except OSError as exc:
        raise CliError(f"could not write {config.path}: {exc}") from exc

    runner = Runner(config, clock_file=opts.get("clock_file"))
    try:
        runner.run()
    except RunnerConflict as exc:
        raise ConflictError(str(exc)) from exc
    except RuntimeOwnershipError as exc:
        raise CliError(str(exc), code="not_permitted") from exc
    return {"state": "stopped"}


def _start_detached(
    ctx: CliContext, opts: dict[str, Any], timeout: float
) -> dict[str, Any]:
    if _is_running(ctx):
        raise ConflictError("a runner is already running")
    argv = [
        sys.executable,
        "-m",
        "smart_panel.cli.main",
        "--home",
        str(ctx.home),
        "run",
    ]
    if "sink" in opts:
        argv += ["--sink", opts["sink"]]
    if "files_dir" in opts:
        argv += ["--files-dir", opts["files_dir"]]
    if "clock_file" in opts:
        argv += ["--clock-file", opts["clock_file"]]

    env = dict(os.environ)
    env["SMART_PANEL_HOME"] = str(ctx.home)
    subprocess.Popen(
        argv,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
        cwd=str(Path.cwd()),
    )
    return _wait_for_first_frame(ctx, timeout)


def _is_running(ctx: CliContext) -> bool:
    try:
        ctx.call("runner.status")
    except CliError as exc:
        if exc.code == "runner_not_running":
            return False
        raise
    return True


def _wait_for_first_frame(ctx: CliContext, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            data = ctx.call("runner.status")
        except CliError as exc:
            if exc.code == "runner_not_running":
                time.sleep(0.05)
                continue
            raise
        last = data
        frames = data.get("frames") or {}
        if data.get("state") == "running" and int(frames.get("sent") or 0) > 0:
            return data
        time.sleep(0.05)
    raise CliError(
        f"the runner did not send a first frame within {timeout:g} s{_start_diagnostic(last)}",
        code="timeout",
    )


def _start_diagnostic(status: dict[str, Any] | None) -> str:
    """Detail for a start/restart timeout so a stalled start is diagnosable.

    A start can time out either because the new runner never answered the
    control socket (it exited) or because it answered but stayed short of a
    first frame (still ``starting``, or up with ``sent`` at zero). Those need
    different fixes, and the bare timeout message cannot tell them apart, so
    report the last observed ``status``.
    """
    if status is None:
        return " (the runner never answered the control socket)"
    frames = status.get("frames") or {}
    return (
        f" (state={status.get('state')!r}, sent={frames.get('sent')}, "
        f"plugins={status.get('plugins')}, errors={status.get('errors')})"
    )


def _wait_for_stop(ctx: CliContext, timeout: float) -> None:
    """Wait until the runner stops answering *and* has fully torn down.

    ``runner.status`` stops answering as soon as the control server is closed,
    but ``core/runner.py::_teardown`` closes the control server *before* it
    joins its loops and releases the single-instance lock and pid file. Spawning
    the replacement inside that window (which ``restart`` does) can hand the new
    runner a lock the old one has not released, so it exits without ever sending
    a frame. Waiting for the pid file to clear the process makes "stopped" mean
    the process really is gone.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_running(ctx) and not _runner_process_alive():
            return
        time.sleep(0.05)
    raise CliError("the runner did not stop within the timeout", code="timeout")


def _runner_process_alive() -> bool:
    """Whether ``<runtime>/runner.pid`` still names a live process."""
    try:
        raw = paths.runner_pid().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:  # an out-of-range pid or another kill failure: not our runner
        return False
    return True


HANDLERS: dict[tuple[str, ...], Any] = {
    ("run",): cmd_run,
    ("start",): cmd_start,
    ("stop",): cmd_stop,
    ("restart",): cmd_restart,
    ("reload",): cmd_reload,
    ("status",): cmd_status,
}

