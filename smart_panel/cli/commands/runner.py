"""Runner lifecycle commands: run, start, stop, restart, reload, status (T055).

``run`` is the foreground entry point the service unit and ``start`` both use.
``start`` detaches a ``run`` with ``start_new_session=True`` and waits for the
socket and the first frame (or the timeout), exiting 6 when a runner is already
running. Every other command is one control request.

A start that times out says which of the two failures it was: the detached child
is kept, so the wait can fail fast — with the child's exit status as the error
code, and its captured stderr under ``SMART_PANEL_TESTING=1`` — when the child
is already dead, and otherwise report whether the child is still running.

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
from smart_panel.cli.output import EXIT_CODE_NAMES, CliError, ConflictError, UsageError
from smart_panel.core import paths

DEFAULT_START_TIMEOUT_S = 10.0
_SINK_CHOICES = ("hid", "files", "null")
_START_OPTS = {"sink": str, "files-dir": str, "clock-file": str}
# The two codes a start poll reads as "not up yet": the socket is not there, or
# it is there and has not answered. Either can simply mean the child is still
# coming up, so the wait keeps polling to its deadline — unless the child has
# already exited, which no amount of waiting will fix.
_NOT_UP_CODES = ("runner_not_running", "timeout")


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
    try:
        ctx.call("runner.stop", args)
    except CliError as exc:
        # A runner that is mid-teardown has already closed the control socket,
        # and a starved one answers nothing, so the request itself fails with
        # runner_not_running or timeout while ``runner.pid`` still names a live
        # process. Reporting that as "not running" claims a stop that has not
        # happened; the wait below decides, and it checks the pid as well as
        # the socket. With no live process it is the documented exit 3.
        if exc.code not in _NOT_UP_CODES or not _runner_process_alive():
            raise
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
    stderr_sink, stderr_log = _child_stderr_sink(ctx)
    try:
        child = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=stderr_sink,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=str(Path.cwd()),
        )
    finally:
        if stderr_log is not None:
            stderr_sink.close()  # the child holds its own copy of the descriptor
    # The child is kept so the wait can tell a dead child from a slow one: its
    # exit is the one outcome no amount of waiting turns into a first frame.
    return _wait_for_first_frame(ctx, timeout, child=child, stderr_log=stderr_log)


def _child_stderr_sink(ctx: CliContext) -> tuple[Any, Path | None]:
    """Where a detached child's stderr goes, and the file when it is captured.

    A detached runner's stderr has nowhere to go, so its own failure message —
    ``smart-panel: another runner already holds .../runner.lock``, say — is
    discarded and the parent is left with a bare timeout. Under
    ``SMART_PANEL_TESTING=1`` it is captured to ``<home>/start-stderr.log`` so
    that a start which dies leaves the child's message behind; in production it
    stays ``DEVNULL``. Diagnostics must never be what breaks a start, so a sink
    that cannot be opened falls back to ``DEVNULL``.
    """
    if os.environ.get("SMART_PANEL_TESTING") != "1":
        return subprocess.DEVNULL, None
    path = ctx.home / "start-stderr.log"
    try:
        ctx.home.mkdir(parents=True, exist_ok=True)
        return path.open("wb"), path
    except OSError:
        return subprocess.DEVNULL, None


def _is_running(ctx: CliContext) -> bool:
    """Whether a runner is on the control socket — including a silent one.

    ``runner_not_running`` means the socket is gone, so no runner is there.
    ``timeout`` means the socket accepted the connection but the runner did not
    answer in time: that is a runner which is starved or mid-teardown and still
    holds the single-instance lock, so it counts as running. Calling it "gone"
    is what handed a second ``start`` a lock the live process still held — its
    child exited 6 and the parent reported a timeout instead of the conflict.
    """
    try:
        ctx.call("runner.status")
    except CliError as exc:
        if exc.code == "timeout":
            return True
        if exc.code == "runner_not_running":
            return False
        raise
    return True


def _wait_for_first_frame(
    ctx: CliContext,
    timeout: float,
    *,
    child: Any = None,
    stderr_log: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            data = ctx.call("runner.status")
        except CliError as exc:
            if exc.code in _NOT_UP_CODES:
                _raise_if_child_exited(child, stderr_log)
                time.sleep(0.05)
                continue
            raise
        last = data
        frames = data.get("frames") or {}
        if data.get("state") == "running" and int(frames.get("sent") or 0) > 0:
            return data
        time.sleep(0.05)
    raise CliError(
        f"the runner did not send a first frame within {timeout:g} s"
        f"{_start_diagnostic(last, child)}",
        code="timeout",
    )


def _raise_if_child_exited(child: Any, stderr_log: Path | None) -> None:
    """Fail a start as soon as the child it spawned is already gone.

    The common case is the child losing the single-instance lock (exit 6), but
    any early exit is worth saying out loud: the status becomes the error code
    (the child is this same CLI, so its status *is* a documented code) and,
    with its stderr captured, its own message rides along.
    """
    if child is None:
        return
    status = child.poll()
    if status is None:
        return
    name = EXIT_CODE_NAMES.get(status)
    label = f" ({name})" if name is not None else ""
    raise CliError(
        f"the runner exited with status {status}{label} before sending a first frame"
        f"{_stderr_excerpt(stderr_log)}",
        code=_child_error_code(status),
    )


def _child_error_code(status: int) -> str:
    """The documented error code for a departed child's exit status.

    The child is ``smart_panel.cli.main run``, so its status is one of the
    documented CLI exit codes. Reporting it keeps ``start`` honest: a child
    that lost the lock exits 6, so ``start`` is a ``conflict`` (``cli.md``:
    "Exit 6 if already running"), not an anonymous timeout.
    """
    name = EXIT_CODE_NAMES.get(status, "internal")
    if name in ("ok", "error"):
        return "internal"
    return name


def _stderr_excerpt(stderr_log: Path | None, *, limit: int = 160) -> str:
    """The captured child's own last stderr line, for a start that died."""
    if stderr_log is None:
        return ""
    try:
        text = stderr_log.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    line = text.splitlines()[-1].strip()
    if len(line) > limit:
        line = line[: limit - 3] + "..."
    return f": {line}"


def _start_diagnostic(status: dict[str, Any] | None, child: Any = None) -> str:
    """Detail for a start/restart timeout so a stalled start is diagnosable.

    A start can time out either because the new runner never answered the
    control socket (it exited) or because it answered but stayed short of a
    first frame (still ``starting``, or up with ``sent`` at zero). Those need
    different fixes, and the bare timeout message cannot tell them apart, so
    report the last observed ``status`` — and, when ``start`` spawned the
    child, whether that child is still running or already dead.
    """
    if status is None:
        return f" (the runner never answered the control socket{_child_state(child)})"
    frames = status.get("frames") or {}
    return (
        f" (state={status.get('state')!r}, sent={frames.get('sent')}, "
        f"plugins={status.get('plugins')}, errors={status.get('errors')}"
        f"{_child_state(child)})"
    )


def _child_state(child: Any) -> str:
    """``; the child ...`` for the diagnostic, empty without a spawned child."""
    if child is None:
        return ""
    status = child.poll()
    if status is None:
        return "; the child is still running"
    return f"; the child exited with status {status}"


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

