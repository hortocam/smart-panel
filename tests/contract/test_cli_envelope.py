"""Contract test for the CLI envelope and exit codes (T036, FR-005).

``contracts/cli.md`` fixes two things the whole agent surface depends on: the
JSON envelope (``{"ok": true, "data": ...}`` / ``{"ok": false, "error": {...}}``,
with failures on **stdout** in ``--json`` mode and the message on stderr in human
mode), and the exit-code table (0 ok, 1 error, 2 invalid_input, 3
runner_not_running, 4 panel_unavailable, 5 not_permitted, 6 conflict, 7
plugin_error). Argument errors use the same envelope.

The envelope is exercised both directly (the pure builders and the table) and
through the **real CLI as a subprocess**, so a regression in ``main`` — an
argument error escaping as a traceback, a failure printing to the wrong stream —
fails here rather than only being noticed by an agent in production.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from smart_panel.cli import output
from smart_panel.core.control import protocol

REPO_ROOT = Path(__file__).resolve().parents[2]

# The documented table from contracts/cli.md, verbatim.
DOCUMENTED_EXIT_CODES = {
    0: "ok",
    1: "error",
    2: "invalid_input",
    3: "runner_not_running",
    4: "panel_unavailable",
    5: "not_permitted",
    6: "conflict",
    7: "plugin_error",
}


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp base: AF_UNIX socket paths are capped near 108 bytes."""
    base = Path(tempfile.mkdtemp(prefix="cli-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def cli_env(tmp_path: Path, short_tmp: Path) -> dict[str, str]:
    """A clean environment for a real CLI subprocess (no runner, its own home)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["SMART_PANEL_HOME"] = str(tmp_path / "home")
    env["SMART_PANEL_RUNTIME"] = str(short_tmp / "runtime")
    env.pop("SMART_PANEL_JSON", None)
    env.pop("SMART_PANEL_RUNNER_UID", None)
    return env


def run_cli(env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real CLI in a subprocess and capture its streams."""
    return subprocess.run(
        [sys.executable, "-m", "smart_panel.cli.main", *args],
        capture_output=True,
        text=True,
        env=dict(env),
        cwd=str(REPO_ROOT),
        timeout=60,
    )


def _stdout_json(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


# --------------------------------------------------------- pure envelope shape


def test_success_envelope_shape() -> None:
    assert output.success_envelope({"value": 1}) == {"ok": True, "data": {"value": 1}}
    assert output.success_envelope() == {"ok": True, "data": {}}


def test_failure_envelope_shape() -> None:
    assert output.failure_envelope("invalid_input", "nope", "display.fps") == {
        "ok": False,
        "error": {"code": "invalid_input", "message": "nope", "setting": "display.fps"},
    }
    # The optional ``setting`` key is omitted when absent.
    assert output.failure_envelope("internal", "boom")["error"] == {
        "code": "internal",
        "message": "boom",
    }


def test_the_exit_code_table_is_the_protocols_own() -> None:
    # The CLI must not own a second copy of the table (T044 owns it).
    assert output.EXIT_CODES == protocol.EXIT_CODES
    assert output.EXIT_CODE_NAMES == protocol.EXIT_CODE_NAMES
    for code, name in DOCUMENTED_EXIT_CODES.items():
        assert protocol.EXIT_CODE_NAMES[code] == name


def test_the_documented_error_codes_map_to_the_documented_exits() -> None:
    assert output.exit_code_for("invalid_input") == 2
    assert output.exit_code_for("not_found") == 2
    assert output.exit_code_for("runner_not_running") == 3
    assert output.exit_code_for("panel_unavailable") == 4
    assert output.exit_code_for("not_permitted") == 5
    assert output.exit_code_for("conflict") == 6
    assert output.exit_code_for("plugin_error") == 7
    assert output.exit_code_for("spool_full") == 1
    assert output.exit_code_for("timeout") == 1
    assert output.exit_code_for("protocol_mismatch") == 1
    assert output.exit_code_for("internal") == 1
    # An unknown code is an unexpected internal error, never a silent success.
    assert output.exit_code_for("something-new") == 1


# ------------------------------------------------------------ real CLI, --json


def test_a_success_prints_the_envelope_and_exits_zero(cli_env: dict[str, str]) -> None:
    proc = run_cli(cli_env, "--json", "version")
    assert proc.returncode == 0
    assert proc.stderr == ""
    body = _stdout_json(proc)
    assert body["ok"] is True
    assert body["data"]["version"]
    assert body["data"]["protocol"] == protocol.PROTOCOL_VERSION


def test_an_unknown_command_is_an_argument_error_envelope(cli_env: dict[str, str]) -> None:
    proc = run_cli(cli_env, "--json", "no-such-command")
    assert proc.returncode == 2
    body = _stdout_json(proc)
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"
    # --json keeps failures on stdout so an agent parses one stream.
    assert proc.stderr == ""


def test_a_missing_argument_is_an_argument_error_envelope(cli_env: dict[str, str]) -> None:
    # ``config set`` needs PATH and VALUE; the usage error must use the envelope.
    proc = run_cli(cli_env, "--json", "config", "set", "display.fps")
    assert proc.returncode == 2
    body = _stdout_json(proc)
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_input"


def test_a_command_needing_a_runner_is_exit_three(cli_env: dict[str, str]) -> None:
    proc = run_cli(cli_env, "--json", "status")
    assert proc.returncode == 3
    body = _stdout_json(proc)
    assert body["ok"] is False
    assert body["error"]["code"] == "runner_not_running"


def test_smart_panel_json_env_enables_the_envelope(cli_env: dict[str, str]) -> None:
    cli_env = dict(cli_env)
    cli_env["SMART_PANEL_JSON"] = "1"
    proc = run_cli(cli_env, "version")
    assert proc.returncode == 0
    assert _stdout_json(proc)["ok"] is True


# -------------------------------------------------------------- human streams


def test_human_mode_writes_the_failure_to_stderr(cli_env: dict[str, str]) -> None:
    proc = run_cli(cli_env, "status")
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert proc.stderr.strip() != ""


def test_quiet_suppresses_human_output_but_keeps_the_exit_code(
    cli_env: dict[str, str],
) -> None:
    proc = run_cli(cli_env, "--quiet", "status")
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_human_success_prints_key_value_lines(cli_env: dict[str, str]) -> None:
    proc = run_cli(cli_env, "version")
    assert proc.returncode == 0
    assert "version: " in proc.stdout
    assert "protocol: " in proc.stdout
