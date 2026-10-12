"""Service installation: a systemd user unit (Linux) or launchd agent (macOS) (T060).

``smart-panel service install`` generates and installs the platform's unit that
keeps the runner alive (``Restart=on-failure``) and recreates the shared runtime
directory **as the service account** at every start — a systemd ``ExecStartPre``
on Linux, and a ``/bin/sh -c 'mkdir -p ... && exec ...'`` wrapper in the launchd
``ProgramArguments`` on macOS, which has no pre-start hook of its own.

The generator functions are pure: :func:`systemd_unit` and :func:`launchd_plist`
return the exact file text, so ``tests/unit/test_service.py`` pins the unit
contents without touching the real ``~/.config/systemd``. Installing, enabling
and the systemd/launchd invocations are the only side effects, and they are
injectable (``run=``) so nothing in the suite shells out to a service manager.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from smart_panel.core import paths

SERVICE_NAME = "smart-panel"
LINUX_UNIT_NAME = "smart-panel.service"
MACOS_LABEL = "com.hortocam.smart-panel"
DEFAULT_RESTART_SEC = 5

RunCommand = Callable[[Sequence[str]], int]


class ServiceError(RuntimeError):
    """A service operation that failed; carries the CLI error code to report."""

    code = "internal"


@dataclass
class ServiceSpec:
    """Everything the generated unit needs to know.

    Args:
        home: ``<home>`` the runner reads its config from.
        runtime: The shared runtime directory the unit recreates on start.
        program: The executable the service runs (defaults to the console
            script next to the running interpreter, or ``python -m``).
        args: The command the program is invoked with (``run``).
        label: systemd unit / launchd label name.
    """

    home: Path
    runtime: Path
    program: str
    args: tuple[str, ...] = ("run",)
    label: str = SERVICE_NAME
    restart_sec: int = DEFAULT_RESTART_SEC
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def exec_line(self) -> str:
        """The ``ExecStart`` command line: the program plus its arguments."""
        parts = [self.program, *self.args]
        return " ".join(parts)

    def unit_environment(self) -> dict[str, str]:
        """The environment the unit exports (home, runtime, overrides)."""
        env = {
            "SMART_PANEL_HOME": str(self.home),
            "SMART_PANEL_RUNTIME": str(self.runtime),
        }
        env.update(self.environment)
        return env

    def pre_start_command(self) -> str:
        """The command that recreates the runtime directory before the runner."""
        return f"/bin/mkdir -p {self.runtime}"


def default_program() -> str:
    """The program path to run: the console script beside the interpreter."""
    candidate = Path(sys.executable).with_name(f"{SERVICE_NAME}")
    if candidate.exists():
        return str(candidate)
    found = shutil.which(SERVICE_NAME)
    if found:
        return found
    return sys.executable  # a bare interpreter; the CLI adds ``-m`` via args


def default_spec(
    *,
    home: Path | None = None,
    runtime: Path | None = None,
    program: str | None = None,
) -> ServiceSpec:
    """Build the spec for this host from the current environment."""
    args: tuple[str, ...] = ("run",)
    chosen = program or default_program()
    if chosen == sys.executable:
        args = ("-m", "smart_panel.cli.main", "run")
    return ServiceSpec(
        home=Path(home) if home is not None else paths.home(),
        runtime=Path(runtime) if runtime is not None else paths.runtime(),
        program=chosen,
        args=args,
    )


# --------------------------------------------------------------------- units


def systemd_unit(spec: ServiceSpec) -> str:
    """Render the systemd user unit for ``spec`` (Linux)."""
    env_lines = "\n".join(
        f"Environment={key}={value}" for key, value in spec.unit_environment().items()
    )
    return (
        "[Unit]\n"
        f"Description={SERVICE_NAME} status display runner\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_lines}\n"
        f"ExecStartPre={spec.pre_start_command()}\n"
        f"ExecStart={spec.exec_line}\n"
        "Restart=on-failure\n"
        f"RestartSec={spec.restart_sec}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def launchd_plist(spec: ServiceSpec) -> str:
    """Render the launchd agent plist for ``spec`` (macOS).

    launchd has no pre-start hook, so the runtime-directory creation is folded
    into the program arguments as ``sh -c 'mkdir -p ... && exec ...'``; a failed
    exit is restarted by ``KeepAlive.SuccessfulExit=false`` (``Restart=on-failure``).
    """
    shell_command = f"{spec.pre_start_command()} && exec {spec.exec_line}"
    payload = {
        "Label": MACOS_LABEL,
        "ProgramArguments": ["/bin/sh", "-c", shell_command],
        "EnvironmentVariables": spec.unit_environment(),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
    }
    return plistlib.dumps(payload).decode("utf-8")


def unit_text(spec: ServiceSpec, *, platform: str | None = None) -> str:
    """The unit/plist text for ``spec`` on ``platform`` (defaults to this host)."""
    if _platform(platform) == "darwin":
        return launchd_plist(spec)
    return systemd_unit(spec)


def unit_path(spec: ServiceSpec, *, platform: str | None = None, home: Path | None = None) -> Path:
    """Where the unit/plist is installed for ``spec``."""
    base = Path(home) if home is not None else Path.home()
    if _platform(platform) == "darwin":
        return base / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
    return base / ".config" / "systemd" / "user" / LINUX_UNIT_NAME


def _platform(platform: str | None = None) -> str:
    if platform is not None:
        return platform
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------- operations


def install(
    spec: ServiceSpec,
    *,
    target: Path | None = None,
    platform: str | None = None,
    run: RunCommand | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Write the unit, create the runtime directory and enable the service.

    Args:
        target: Where to write the unit; defaults to :func:`unit_path`.
        platform: Override platform detection (tests).
        run: Callable executing one command and returning its exit code; when
            ``None`` the commands are reported but not executed.
        dry_run: Render and report everything, but touch nothing.

    Returns:
        A result dict with the unit path, the enable commands and, when ``run``
        is supplied, their exit codes plus any headless-linger hint.
    """
    os_name = _platform(platform)
    destination = target or unit_path(spec, platform=os_name)
    text = unit_text(spec, platform=os_name)

    if not dry_run:
        Path(spec.runtime).mkdir(parents=True, exist_ok=True)
        os.chmod(spec.runtime, 0o755)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    commands = _enable_commands(spec, destination, os_name)
    results: list[dict[str, object]] = []
    if run is not None and not dry_run:
        for command in commands:
            code = run(command)
            results.append({"command": list(command), "exit_code": code})
            if code != 0:
                raise ServiceError(
                    f"command {list(command)} failed with exit code {code}"
                )

    return {
        "installed": True,
        "unit": str(destination),
        "commands": [list(command) for command in commands],
        "results": results,
        "linger_hint": linger_hint() if os_name == "linux" else None,
    }


def uninstall(
    spec: ServiceSpec,
    *,
    target: Path | None = None,
    platform: str | None = None,
    run: RunCommand | None = None,
) -> dict[str, object]:
    """Disable the service and remove its unit file."""
    os_name = _platform(platform)
    destination = target or unit_path(spec, platform=os_name)
    commands = _disable_commands(spec, destination, os_name)
    if run is not None:
        for command in commands:
            run(command)
    destination.unlink(missing_ok=True)
    return {"uninstalled": True, "unit": str(destination), "commands": [list(c) for c in commands]}


def status(
    spec: ServiceSpec,
    *,
    target: Path | None = None,
    platform: str | None = None,
    run: RunCommand | None = None,
    output: Callable[[Sequence[str]], str] | None = None,
) -> dict[str, object]:
    """Report whether the unit is installed, active and enabled."""
    os_name = _platform(platform)
    destination = target or unit_path(spec, platform=os_name)
    installed = destination.exists()
    active: str | None = None
    enabled: str | None = None
    if installed and output is not None:
        if os_name == "darwin":
            active = output(["launchctl", "list", MACOS_LABEL]).strip() or None
        else:
            active = output(["systemctl", "--user", "is-active", SERVICE_NAME]).strip() or None
            enabled = output(["systemctl", "--user", "is-enabled", SERVICE_NAME]).strip() or None
    return {
        "installed": installed,
        "unit": str(destination),
        "active": active,
        "enabled": enabled,
        "linger_hint": linger_hint() if os_name == "linux" else None,
    }


def _enable_commands(spec: ServiceSpec, target: Path, platform: str) -> list[tuple[str, ...]]:
    if platform == "darwin":
        return [("launchctl", "load", "-w", str(target))]
    return [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", SERVICE_NAME),
    ]


def _disable_commands(spec: ServiceSpec, target: Path, platform: str) -> list[tuple[str, ...]]:
    if platform == "darwin":
        return [("launchctl", "unload", "-w", str(target))]
    return [("systemctl", "--user", "disable", "--now", SERVICE_NAME)]


def linger_hint() -> str | None:
    """The one-time ``loginctl enable-linger`` hint on a headless Linux host."""
    if _platform() != "linux":
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.geteuid())
    return (
        f"headless host: run `loginctl enable-linger {user}` so the user service "
        "starts at boot without an interactive login"
    )


def default_runner() -> RunCommand:
    """A ``run`` callable that executes a command quietly and returns its code."""

    def run(command: Sequence[str]) -> int:
        completed = subprocess.run(list(command), capture_output=True, check=False)
        return completed.returncode

    return run


def default_capture() -> Callable[[Sequence[str]], str]:
    """A callable that runs a command and returns its stdout (for ``is-active``)."""

    def capture(command: Sequence[str]) -> str:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, check=False
        )
        return completed.stdout

    return capture
