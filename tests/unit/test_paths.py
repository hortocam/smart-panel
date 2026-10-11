"""Unit tests for ``smart_panel.core.paths`` (T016).

These pin the location rules the runner, CLI and spool depend on: the home
directory is the account's own 0700 config dir, the runtime directory is a
single shared path for every account (so allowed non-owner accounts can find
the control socket), and no CLI-side helper ever creates the runtime directory
(the runner and ``service install`` are the only creators).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from smart_panel.core import paths


def test_home_uses_env_override(tmp_home: Path) -> None:
    assert paths.home() == tmp_home


def test_home_default_is_config_smart_panel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SMART_PANEL_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.home() == tmp_path / ".config" / "smart-panel"


def test_home_created_with_mode_0700(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "home"
    monkeypatch.setenv("SMART_PANEL_HOME", str(target))
    home = paths.home()
    assert home == target
    assert home.is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_runtime_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path / "rt"))
    assert paths.runtime() == tmp_path / "rt"


def test_runtime_default_is_shared_for_all_uids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMART_PANEL_RUNTIME", raising=False)
    monkeypatch.setattr(paths.os, "geteuid", lambda: 1000)
    first = paths.runtime()
    monkeypatch.setattr(paths.os, "geteuid", lambda: 2000)
    second = paths.runtime()
    assert first == second == Path("/tmp/smart-panel")


def test_cli_side_helpers_do_not_create_the_runtime_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "rt"
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(runtime))
    paths.runtime()
    paths.control_sock()
    paths.runner_lock()
    paths.runner_pid()
    paths.spool()
    assert not runtime.exists()


def test_spool_defaults_under_the_runtime_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path / "rt"))
    assert paths.spool() == tmp_path / "rt" / "spool"


def test_spool_honours_paths_spool_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path / "rt"))
    assert paths.spool("/mnt/disk/spool") == Path("/mnt/disk/spool")


def test_ensure_runtime_creates_directory_mode_0755(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "rt"
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(runtime))
    assert paths.ensure_runtime() == runtime
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755


def test_ensure_runtime_refuses_a_directory_owned_by_another_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir()
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(runtime))
    other_uid = os.geteuid() ^ 1
    with pytest.raises(paths.RuntimeOwnershipError) as excinfo:
        paths.ensure_runtime(uid=other_uid)
    assert str(runtime) in str(excinfo.value)


def test_ensure_runtime_accepts_a_directory_owned_by_its_own_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "rt"
    runtime.mkdir()
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(runtime))
    assert paths.ensure_runtime() == runtime


def test_ensure_spool_creates_sticky_directory_mode_1733(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path / "rt"))
    spool = paths.ensure_spool()
    assert spool == tmp_path / "rt" / "spool"
    assert stat.S_IMODE(spool.stat().st_mode) == 0o1733


def test_home_and_runtime_path_helpers(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_home / "rt"))
    assert paths.config_file() == tmp_home / "config.json"
    assert paths.secrets_file() == tmp_home / "secrets.json"
    assert paths.state_db() == tmp_home / "state.db"
    assert paths.state_dir() == tmp_home / "state"
    assert paths.plugins_dir() == tmp_home / "plugins"
    assert paths.pending_spool() == tmp_home / "spool-pending"
    assert paths.log_file() == tmp_home / "runner.log"
    assert paths.control_sock() == tmp_home / "rt" / "control.sock"
    assert paths.runner_lock() == tmp_home / "rt" / "runner.lock"
    assert paths.runner_pid() == tmp_home / "rt" / "runner.pid"
