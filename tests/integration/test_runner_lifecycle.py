"""Integration test for the runner lifecycle through the real CLI (T039).

Everything here drives ``python -m smart_panel.cli.main`` as a subprocess against
a file sink and a fake clock (``--clock-file``, honored only under
``SMART_PANEL_TESTING=1``), so the JSON envelope, the exit codes and the frame
pipeline are exercised end to end on a host with no panel attached:

* ``start`` brings a configured ``text`` instance up and ``status`` reports the
  stable fields ``contracts/cli.md`` names;
* a second ``start`` exits 6;
* an invalid ``config set`` exits 2, names ``display.fps`` and changes nothing;
* a ``config set`` of the text instance shows up in ``preview`` within 5 s, with
  no blank or partial frame in the ``FileSink`` frame sequence across the change;
* ``restart`` returns content within 10 s and ``reload`` of a broken file keeps
  the last good configuration;
* with the runner **down**, ``config set`` of an instance naming an unknown
  plugin is refused with exit 2 (``invalid_input`` / ``plugins.<name>.plugin``)
  and persists nothing — the same FR-006 guard the runner applies live — while a
  registered built-in instance is still written through;
* ``stop`` exits 0 and leaves no runner.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)

# The stable ``status`` fields from contracts/cli.md (plus ``panel.message``,
# the extra human hint a wave-1 review flagged beyond the contract's stable set).
STATUS_FIELDS = (
    "state",
    "uptime_s",
    "pid",
    "version",
    "panel",
    "frames",
    "layout",
    "plugins",
    "spool",
    "errors",
)
PANEL_FIELDS = ("state", "sink", "last_error", "reconnects")
FRAMES_FIELDS = ("fps", "target_fps", "governor_level", "last_bytes", "quality")


# -------------------------------------------------------------------- fixtures


class CliHost:
    """A CLI host: the environment, the paths and subprocess helpers."""

    def __init__(self, base: Path, home: Path) -> None:
        self.base = base
        self.home = home
        self.runtime = base / "runtime"
        self.frames = base / "frames"
        self.clock = base / "clock"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(self.runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env

    def cli(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def data(
        self, *args: str, timeout: float = 60.0
    ) -> tuple[dict, subprocess.CompletedProcess[str]]:
        proc = self.cli(*args, timeout=timeout)
        return json.loads(proc.stdout), proc

    def seed_config(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "display": {
                        "fps": 15,
                        "brightness": 50,
                        "sink": "files",
                        "files_dir": str(self.frames),
                        "min_text_px": 24,
                    },
                    "layout": {
                        "regions": [
                            {
                                "name": "main",
                                "x": 0,
                                "y": 0,
                                "w": CANVAS[0],
                                "h": CANVAS[1],
                                "instance": "text",
                            }
                        ],
                        "overlays": [],
                    },
                    "plugins": {
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "initial", "size": 120},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.clock.write_text("0\n", encoding="utf-8")

    def start(self, *, timeout: str = "15") -> subprocess.CompletedProcess[str]:
        return self.cli(
            "--json",
            "--timeout",
            timeout,
            "start",
            "--sink",
            "files",
            "--files-dir",
            str(self.frames),
            "--clock-file",
            str(self.clock),
            timeout=float(timeout) + 30,
        )

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "stop", timeout=30)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="life-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home")
    cli_host.seed_config()
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def read_latest(path: Path) -> PILImage.Image:
    with PILImage.open(path) as image:
        image.load()
        return image.convert("RGB")


def is_non_blank(image: PILImage.Image) -> bool:
    """A frame is non-blank when the text painted at least one bright pixel."""
    high = image.convert("L").getextrema()[1]  # type: ignore[index]
    return int(high) > 200


# ----------------------------------------------------------------------- tests


def test_lifecycle_start_status_second_start_and_stop(host: CliHost) -> None:
    started, proc = host.data(*_start_args(host))
    assert proc.returncode == 0, proc.stderr
    assert started["ok"] is True
    assert started["data"]["state"] == "running"
    assert started["data"]["frames"]["sent"] > 0

    status, proc = host.data("--json", "status")
    assert proc.returncode == 0
    for field in STATUS_FIELDS:
        assert field in status["data"], f"status is missing {field!r}"
    panel = status["data"]["panel"]
    for field in PANEL_FIELDS:
        assert field in panel
    assert panel["sink"] == "files"
    for field in FRAMES_FIELDS:
        assert field in status["data"]["frames"]
    assert status["data"]["frames"]["target_fps"] == 15
    assert status["data"]["layout"]["regions"] == [{"name": "main", "instance": "text"}]
    assert status["data"]["plugins"]["text"]["state"] == "ok"
    assert status["data"]["spool"]["pending"] == 0
    assert isinstance(status["data"]["errors"], list)

    # A second runner is a conflict (exit 6) and the first keeps running.
    second, proc = host.data("--json", "start")
    assert proc.returncode == 6
    assert second["error"]["code"] == "conflict"

    proc = host.stop()
    assert proc.returncode == 0
    stopped = json.loads(proc.stdout)
    assert stopped["ok"] is True

    gone, proc = host.data("--json", "status")
    assert proc.returncode == 3
    assert gone["error"]["code"] == "runner_not_running"


def test_an_invalid_config_set_is_refused_and_changes_nothing(host: CliHost) -> None:
    _require_started(host)

    failed, proc = host.data("--json", "config", "set", "display.fps", "500")
    assert proc.returncode == 2
    assert failed["ok"] is False
    assert failed["error"]["code"] == "invalid_input"
    # The envelope names the offender through ``setting`` (contracts/cli.md).
    assert failed["error"]["setting"] == "display.fps"
    assert failed["error"]["message"]

    # The running config is unchanged: the target fps is still the default.
    status, _ = host.data("--json", "status")
    assert status["data"]["frames"]["target_fps"] == 15
    value, _ = host.data("--json", "config", "get", "display.fps")
    assert value["data"]["value"] == 15


def test_a_config_set_appears_in_preview_without_a_partial_frame(host: CliHost) -> None:
    _require_started(host)

    before = host.base / "before.png"
    _preview(host, before)
    reference = read_latest(before)
    assert reference.size == CANVAS
    assert is_non_blank(reference)

    # Watch the FileSink frame sequence before and across the change: every frame
    # must be a complete 1920x462 canvas with content on it (FileSink writes
    # atomically, so a partial frame is impossible and must never be observed).
    observed = sample_frames(host, count=5)
    started = time.monotonic()
    changed, proc = host.data("--json", "config", "set", "plugins.text.settings.text", "HELLO")
    assert proc.returncode == 0 and changed["ok"] is True
    observed += sample_frames(host, count=5)

    after = host.base / "after.png"
    deadline = started + 5.0
    while time.monotonic() < deadline:
        _preview(host, after)
        if after.read_bytes() != before.read_bytes():
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the text change never appeared in preview within 5 s")

    assert time.monotonic() - started <= 5.0
    assert len(observed) >= 10, f"only observed {len(observed)} frames"
    assert set(observed) == {CANVAS}
    assert read_latest(after).size == CANVAS
    assert is_non_blank(read_latest(after))


def test_restart_returns_content(host: CliHost) -> None:
    _require_started(host)

    restarted, proc = host.data("--json", "restart", "--clock-file", str(host.clock), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert restarted["data"]["state"] == "running"
    assert restarted["data"]["frames"]["sent"] > 0

    def has_content() -> bool:
        status, _ = host.data("--json", "status")
        return status.get("ok") is True and status["data"]["frames"]["sent"] > 0

    assert wait_for(has_content, timeout=10.0)


def test_reload_of_a_broken_file_keeps_the_last_good_config(host: CliHost) -> None:
    _require_started(host)
    ok, proc = host.data("--json", "config", "set", "plugins.text.settings.text", "kept")
    assert proc.returncode == 0 and ok["ok"] is True

    # A broken (but valid-JSON, invalid-value) config file.
    (host.home / "config.json").write_text(
        json.dumps({"version": 1, "display": {"fps": 500}}), encoding="utf-8"
    )
    failed, proc = host.data("--json", "reload")
    assert proc.returncode == 2
    assert failed["error"]["code"] == "invalid_input"
    assert failed["error"]["setting"] == "display.fps"

    # The last good configuration is still in effect, live.
    status, _ = host.data("--json", "status")
    assert status["data"]["frames"]["target_fps"] == 15
    value, _ = host.data("--json", "config", "get", "plugins.text.settings.text")
    assert value["data"]["value"] == "kept"


# ------------------------------------------- runner-down config (T056 fallback)


def test_a_runner_down_config_set_of_an_unknown_plugin_is_refused(host: CliHost) -> None:
    # No runner is started (the ``host`` fixture never starts one): the CLI falls
    # back to editing ``config.json`` itself (T056). The FR-006 guard must apply
    # on that path too, or the verdict for one and the same command would depend
    # on whether a runner happened to be running — contracts/cli.md puts "unknown
    # plugin" at exit 2 for the CLI as a whole, with no runner-state carve-out.
    before = (host.home / "config.json").read_bytes()

    refused, proc = host.data("--json", "config", "set", "plugins.doom.plugin", "nosuchplugin")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert refused["ok"] is False
    assert refused["error"]["code"] == "invalid_input"
    assert refused["error"]["setting"] == "plugins.doom.plugin"
    # Nothing was persisted: the same guard the runner applies on the live path.
    assert (host.home / "config.json").read_bytes() == before


def test_a_runner_down_config_set_of_a_whole_dead_instance_is_refused(host: CliHost) -> None:
    before = (host.home / "config.json").read_bytes()

    refused, proc = host.data(
        "--json",
        "config",
        "set",
        "plugins.doom",
        json.dumps({"plugin": "nosuchplugin", "enabled": True}),
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert refused["error"]["code"] == "invalid_input"
    assert refused["error"]["setting"] == "plugins.doom.plugin"
    assert (host.home / "config.json").read_bytes() == before


def test_a_runner_down_config_set_of_a_discovered_plugin_is_applied(host: CliHost) -> None:
    # The guard refuses an unknown plugin, not the runner-down write path itself:
    # a registered built-in instance is still written through and durable.
    applied, proc = host.data(
        "--json",
        "config",
        "set",
        "plugins.hello",
        json.dumps({"plugin": "text", "enabled": True}),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert applied["ok"] is True
    assert applied["data"]["applied"] is True
    document = json.loads((host.home / "config.json").read_text(encoding="utf-8"))
    assert document["plugins"]["hello"]["plugin"] == "text"


# --------------------------------------------------------------------- helpers


def _start_args(host: CliHost) -> list[str]:
    return [
        "--json",
        "--timeout",
        "15",
        "start",
        "--sink",
        "files",
        "--files-dir",
        str(host.frames),
        "--clock-file",
        str(host.clock),
    ]


def _require_started(host: CliHost) -> dict:
    started, proc = host.data(*_start_args(host), timeout=45)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert started["data"]["state"] == "running"
    return started


def _preview(host: CliHost, target: Path) -> None:
    data, proc = host.data("--json", "preview", "--output", str(target))
    assert proc.returncode == 0, proc.stderr
    assert data["data"]["output"] == str(target)


def sample_frames(host: CliHost, *, count: int, spacing: float = 0.05) -> list[tuple[int, int]]:
    """Read the current FileSink frame ``count`` times, asserting it is whole.

    Every observation must decode to the full canvas with content on it, which is
    what "no blank or partial frame in the FileSink frame sequence" means.
    """
    sizes: list[tuple[int, int]] = []
    frame = host.frames / "latest.png"
    for _ in range(count):
        if frame.exists():
            image = read_latest(frame)
            assert image.size == CANVAS
            assert is_non_blank(image)
            sizes.append(image.size)
        time.sleep(spacing)
    return sizes
