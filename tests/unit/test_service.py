"""Unit tests for ``smart_panel.service`` (T060).

``service install`` generates the platform's unit and recreates the shared
runtime directory *as the service account* at every start. These tests pin the
generated text without touching a real ``~/.config/systemd`` or launchd: the
generators are pure strings, and the one side-effecting path (writing the unit
and running the enable commands) is exercised against a temp target with an
injected command runner.
"""

from __future__ import annotations

import plistlib
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from smart_panel import service

HOME = Path("/var/lib/smart-panel")
RUNTIME = Path("/tmp/smart-panel")
PROGRAM = "/usr/bin/smart-panel"


def _spec(runtime: Path = RUNTIME) -> service.ServiceSpec:
    return service.ServiceSpec(home=HOME, runtime=runtime, program=PROGRAM)


# --------------------------------------------------------------- generated unit


def test_systemd_unit_pins_the_service_contract() -> None:
    text = service.systemd_unit(_spec())
    assert "[Unit]" in text
    assert "Description=smart-panel status display runner" in text
    assert "[Service]" in text
    assert "Type=simple" in text
    assert f"Environment=SMART_PANEL_HOME={HOME}" in text
    assert f"Environment=SMART_PANEL_RUNTIME={RUNTIME}" in text
    # The runtime directory is recreated as the service account on every start.
    assert f"ExecStartPre=/bin/mkdir -p {RUNTIME}" in text
    assert f"ExecStart={PROGRAM} run" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text
    assert "[Install]" in text
    assert "WantedBy=default.target" in text


def test_launchd_plist_folds_the_pre_start_into_the_program_arguments() -> None:
    document = plistlib.loads(service.launchd_plist(_spec()).encode("utf-8"))
    assert document["Label"] == service.MACOS_LABEL
    arguments = document["ProgramArguments"]
    assert arguments[0] == "/bin/sh"
    assert arguments[1] == "-c"
    assert f"mkdir -p {RUNTIME}" in arguments[2]
    assert f"exec {PROGRAM} run" in arguments[2]
    assert document["RunAtLoad"] is True
    # Restart=on-failure's launchd equivalent.
    assert document["KeepAlive"] == {"SuccessfulExit": False}
    assert document["EnvironmentVariables"]["SMART_PANEL_HOME"] == str(HOME)


def test_unit_path_is_per_platform(tmp_path: Path) -> None:
    spec = _spec()
    linux = service.unit_path(spec, platform="linux", home=tmp_path)
    assert linux == tmp_path / ".config" / "systemd" / "user" / "smart-panel.service"
    macos = service.unit_path(spec, platform="darwin", home=tmp_path)
    assert macos == tmp_path / "Library" / "LaunchAgents" / f"{service.MACOS_LABEL}.plist"


def test_unit_text_selects_the_platform(tmp_path: Path) -> None:
    spec = _spec()
    assert "[Service]" in service.unit_text(spec, platform="linux")
    assert "<plist" in service.unit_text(spec, platform="darwin")


# ------------------------------------------------------------------ operations


def test_install_writes_the_unit_creates_the_runtime_and_enables(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    target = tmp_path / "smart-panel.service"
    spec = _spec(runtime)
    ran: list[tuple[str, ...]] = []

    def run(command: Sequence[str]) -> int:
        ran.append(tuple(command))
        return 0

    result: dict[str, Any] = service.install(spec, target=target, platform="linux", run=run)

    assert result["installed"] is True
    assert result["unit"] == str(target)
    assert target.read_text(encoding="utf-8") == service.systemd_unit(spec)
    assert runtime.is_dir()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755
    commands = result["commands"]
    assert commands[0] == ["systemctl", "--user", "daemon-reload"]
    assert commands[1] == ["systemctl", "--user", "enable", "--now", "smart-panel"]
    assert ran == [tuple(command) for command in commands]


def test_install_can_be_dry_run(tmp_path: Path) -> None:
    runtime = tmp_path / "rt"
    target = tmp_path / "unit.service"
    result = service.install(_spec(runtime), target=target, platform="linux", dry_run=True)
    assert result["installed"] is True
    assert not target.exists()
    assert not runtime.exists()


def test_install_reports_a_failing_enable(tmp_path: Path) -> None:
    target = tmp_path / "unit.service"
    with pytest.raises(service.ServiceError) as excinfo:
        service.install(
            _spec(tmp_path / "rt"),
            target=target,
            platform="linux",
            run=lambda command: 1,
        )
    assert excinfo.value.code == "internal"


def test_uninstall_removes_the_unit(tmp_path: Path) -> None:
    target = tmp_path / "unit.service"
    service.install(_spec(tmp_path / "rt"), target=target, platform="linux", dry_run=False)
    assert target.exists()
    result = service.uninstall(_spec(tmp_path / "rt"), target=target, platform="linux")
    assert result["uninstalled"] is True
    assert not target.exists()


def test_status_reports_absent_when_there_is_no_unit(tmp_path: Path) -> None:
    result = service.status(_spec(), target=tmp_path / "missing.service", platform="linux")
    assert result["installed"] is False
    assert result["active"] is None


def test_status_reads_active_and_enabled(tmp_path: Path) -> None:
    target = tmp_path / "unit.service"
    target.write_text(service.systemd_unit(_spec()), encoding="utf-8")

    def output(command: Sequence[str]) -> str:
        if "is-active" in command:
            return "active\n"
        if "is-enabled" in command:
            return "enabled\n"
        return ""

    result = service.status(
        _spec(), target=target, platform="linux", output=output
    )
    assert result["installed"] is True
    assert result["active"] == "active"
    assert result["enabled"] == "enabled"


def test_linger_hint_only_on_a_headless_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(service, "_platform", lambda platform=None: "linux")
    hint = service.linger_hint()
    assert hint is not None
    assert "loginctl enable-linger" in hint

    monkeypatch.setenv("DISPLAY", ":0")
    assert service.linger_hint() is None


def test_default_spec_uses_the_home_and_runtime(tmp_path: Path) -> None:
    spec = service.default_spec(home=tmp_path / "home", runtime=tmp_path / "rt")
    assert spec.home == tmp_path / "home"
    assert spec.runtime == tmp_path / "rt"
    assert spec.program
    assert spec.args[-1] == "run"
    assert spec.exec_line.endswith(" run")


def test_environment_overrides_are_exported() -> None:
    spec = service.ServiceSpec(
        home=HOME,
        runtime=RUNTIME,
        program=PROGRAM,
        environment={"SMART_PANEL_JSON": "1"},
    )
    assert spec.unit_environment()["SMART_PANEL_JSON"] == "1"
    assert "Environment=SMART_PANEL_JSON=1" in service.systemd_unit(spec)


def test_platform_detection_follows_the_host() -> None:
    expected = "darwin" if sys.platform == "darwin" else "linux"
    assert service._platform() == expected
    assert service._platform("darwin") == "darwin"
