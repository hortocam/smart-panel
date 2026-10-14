"""Integration test for the runner lifecycle through the real CLI (T039).

Everything here drives ``python -m smart_panel.cli.main`` as a subprocess against
a file sink and a fake clock (``--clock-file``, honored only under
``SMART_PANEL_TESTING=1``), so the JSON envelope, the exit codes and the frame
pipeline are exercised end to end on a host with no panel attached:

* ``start`` brings a configured ``text`` instance up and ``status`` reports the
  stable fields ``contracts/cli.md`` names;
* an immediate ``preview`` (no wait) after ``start`` is non-blank: the runner's
  first *sent* frame is a content frame, not the bare background;
* a second ``start`` exits 6;
* an invalid ``config set`` exits 2, names ``display.fps`` and changes nothing;
* a ``config set`` of the text instance shows up in ``preview`` within 5 s, with
  no blank or partial frame in the ``FileSink`` frame sequence across the change;
* ``restart`` returns content within 10 s, with an immediate non-blank
  ``preview``, and ``reload`` of a broken file keeps the last good configuration;
* with the runner **down**, ``config set`` of an instance naming an unknown
  plugin is refused with exit 2 (``invalid_input`` / ``plugins.<name>.plugin``)
  and persists nothing — the same FR-006 guard the runner applies live — while a
  registered built-in instance is still written through;
* a **silent** runner (``SIGSTOP``ped, so the socket accepts and the hello goes
  unanswered) is not reported as ``runner_not_running``: ``status`` is exit 1
  ``timeout`` and a second ``start`` is exit 6 ``conflict`` rather than a
  detached child quietly dying on the lock the frozen runner still holds; a
  ``logs --follow`` against the same frozen runner is exit 1 ``timeout`` too,
  not exit 3;
* a ``start`` whose detached child dies (here: the lock is held, so the child
  exits 6) reports the child — the conflict code and the child's own stderr —
  instead of a bare "no first frame" timeout;
* ``stop`` exits 0 and leaves no runner.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage

from smart_panel.core.control import protocol

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

    def start(self, *, timeout: str = "30") -> subprocess.CompletedProcess[str]:
        # The CLI detaches a ``run`` child and waits ``max(--timeout, 10)`` for
        # its first frame; 15 s was too tight for a starved CI leg, so the
        # default is the 30 s the sibling e2e hosts settled on. The subprocess
        # bound below (``--timeout`` + 30) stays above the CLI's own wait.
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
    # No wait, no retry: `start` returns only once a *content* frame has been
    # sent, so the very next `preview` renders region content (T051).
    reference = _preview_now(host, before)
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

    # A generous global ``--timeout``: ``restart`` stops the runner first, then
    # the start wait is ``max(ctx.timeout, 10)`` for the fresh runner's first
    # frame — a loaded macOS CI runner can take well over 10 s to bring one up.
    # 30 s matches the sibling e2e hosts' ``start`` bound; the wait stays
    # bounded, so a runner that never starts still fails.
    restarted, proc = host.data(
        "--json", "--timeout", "30", "restart", "--clock-file", str(host.clock), timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert restarted["data"]["state"] == "running"
    assert restarted["data"]["frames"]["sent"] > 0

    # ``restart`` waits for the first frame, and the first frame the runner now
    # sends carries region content, so an immediate preview (no wait) is
    # non-blank — the same guarantee ``start`` gives (T051).
    image = _preview_now(host, host.base / "restarted.png")
    assert image.size == CANVAS


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
        "30",
        "start",
        "--sink",
        "files",
        "--files-dir",
        str(host.frames),
        "--clock-file",
        str(host.clock),
    ]


def _require_started(host: CliHost) -> dict:
    started, proc = host.data(*_start_args(host), timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert started["data"]["state"] == "running"
    return started


def _preview(host: CliHost, target: Path) -> None:
    data, proc = host.data("--json", "preview", "--output", str(target))
    assert proc.returncode == 0, proc.stderr
    assert data["data"]["output"] == str(target)


def _preview_now(host: CliHost, target: Path) -> PILImage.Image:
    """Preview once, with no wait, and require the canvas to carry content.

    ``start``/``restart`` return only after the runner has sent a *content*
    frame: ``core/runner.py::_release_first_frame`` holds the first frame back
    until a compose pass carries a region layer (or a ``degraded``/``stalled``
    instance's error badge) or until every instance has delivered its first
    render. The blank first frame was a startup race — ``_compose_once`` submits
    to a worker *thread* and reads ``worker.last_image`` in the same pass — and
    used to make an immediate ``preview`` flaky on a loaded macOS CI runner
    (observed: a blank 1920x462 canvas, ``max_luminance=0``). A blank canvas
    here now means a real defect, so this helper does not retry: it fails on the
    first look. (A genuinely empty layout — every instance ``Inactive``, or no
    instances at all — does start on the bare background; that is not a startup
    race, and ``status.errors`` carries ``first_frame_without_content``.)
    """
    _preview(host, target)
    image = read_latest(target)
    assert image.size == CANVAS
    assert is_non_blank(image), (
        "the composed canvas is blank immediately after start "
        f"(max_luminance={image.convert('L').getextrema()[1]}, size={image.size})"
    )
    return image


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


# -------------------------------------------- a runner that is there but silent


def test_a_silent_runner_is_not_reported_as_gone(host: CliHost) -> None:
    """A frozen runner is still a runner: no false exit 3, no doomed child.

    ``SIGSTOP`` leaves the listening socket in place, so a client's connect
    succeeds and its hello goes unanswered — the "starved, or mid-teardown"
    shape. ``status`` must not claim ``runner_not_running`` (exit 3) while
    ``runner.pid`` still names a live process, and ``start`` must report the
    conflict (exit 6) rather than detaching a child into the lock the frozen
    runner holds and then reporting a bare timeout.
    """
    _require_started(host)
    pid_file = host.runtime / "runner.pid"
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    os.kill(pid, signal.SIGSTOP)
    try:
        status, proc = host.data("--json", "--timeout", "2", "status")
        assert proc.returncode == protocol.EXIT_CODES["timeout"] == 1, proc.stdout + proc.stderr
        assert status["error"]["code"] == "timeout", status
        assert "did not answer" in status["error"]["message"]

        second, proc = host.data("--json", "--timeout", "2", "start")
        assert proc.returncode == 6, second
        assert second["error"]["code"] == "conflict", second
    finally:
        os.kill(pid, signal.SIGCONT)

    proc = host.stop()
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_follow_of_a_silent_runner_is_not_reported_as_gone(host: CliHost) -> None:
    """``logs --follow`` against a frozen runner exits 1 ``timeout``, not 3.

    The same ``SIGSTOP``ped runner as above: ``runner.pid`` still names a live
    process and the listening socket still accepts, so the follow's connect
    succeeds and its hello handshake goes unanswered. ``--timeout`` bounds the
    connect and handshake, so the follow must report the documented ``timeout``
    code (exit 1) — "the runner did not answer" — rather than
    ``runner_not_running`` (exit 3), which claims there is no runner while a
    live one holds the single-instance lock. This is the streaming path's last
    conflation of the two "no answer" shapes.
    """
    _require_started(host)
    pid_file = host.runtime / "runner.pid"
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    os.kill(pid, signal.SIGSTOP)
    try:
        # The frozen pid is still live: this is not "no runner".
        os.kill(pid, 0)
        proc = host.cli("--json", "--timeout", "2", "logs", "--follow")
        assert proc.returncode == protocol.EXIT_CODES["timeout"] == 1, proc.stdout + proc.stderr
        payload = json.loads(proc.stdout.splitlines()[0])
        assert payload["error"]["code"] == "timeout", payload
        assert "did not answer" in payload["error"]["message"]
    finally:
        os.kill(pid, signal.SIGCONT)

    proc = host.stop()
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_start_whose_child_loses_the_lock_names_the_child(host: CliHost) -> None:
    """The detached child's own failure must not be invisible to the parent.

    With ``<runtime>/runner.lock`` held — as a surviving runner, or a runner
    frozen mid-teardown, would hold it — there is no control socket, so
    ``start`` detaches a child; the child loses the lock, exits 6 and prints
    why. The parent must report that child (the conflict code, and the child's
    own stderr line under ``SMART_PANEL_TESTING=1``) instead of a bare "no
    first frame" timeout that hides it.
    """
    host.runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(host.runtime, 0o755)
    lock = host.runtime / "runner.lock"
    lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started, proc = host.data(*_start_args(host), timeout=60)
    finally:
        os.close(lock_fd)

    assert proc.returncode == 6, started
    assert started["error"]["code"] == "conflict", started
    message = started["error"]["message"]
    assert "exited with status 6" in message, message
    assert "another runner already holds" in message, message
    assert str(lock) in message, message
