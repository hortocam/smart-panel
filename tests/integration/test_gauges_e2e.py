"""End-to-end test: gauges with the file sink, a fake clock and a fake psutil (T094, US4).

Runs quickstart **Scenario E** against the real CLI and a real runner:

* a ``sys`` ``system`` source publishes ``sys.cpu``, ``sys.mem`` and ``sys.disk:/``
  from the test-only fake ``psutil`` (``SMART_PANEL_FAKE_PSUTIL``, honoured only
  under ``SMART_PANEL_TESTING=1``, like ``--clock-file``);
* four gauges (CPU, MEM, TEMP, DISK) render in a four-up 2x2 grid; TEMP has no
  source metric at all, so it shows the ``no data`` state on every host;
* crossing a configured threshold changes the CPU gauge's colour and it recovers
  when the value comes back down;
* a fifth and sixth gauge adapt the grid to 3x2, and a seventh is rejected with
  exit 2 and a message naming the maximum of 6;
* a value pushed from the CLI (``gauge push``) reaches its gauge;
* a source that stops reporting makes its gauge show the stale state while a
  source that is still reporting stays fresh.
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
FAKE_PSUTIL = Path(__file__).resolve().parent / "fake_psutil.py"
CANVAS = (1920, 462)
MAIN = (192, 0, 1728, 382)
MAIN_BG = (16, 20, 24)  # #101418, the gauges' background
WARN = (230, 167, 0)  # #E6A700
CRIT = (214, 40, 57)  # #D62839

CPU_GAUGE = {
    "id": "cpu",
    "label": "CPU",
    "topic": "sys.cpu",
    "aggregation": "last",
    "window_s": 60,
    "unit": "%",
    "range": [0, 100],
    "style": "dial",
    "thresholds": [{"at": 60, "color": "#E6A700"}, {"at": 85, "color": "#D62839"}],
}
MEM_GAUGE = {
    "id": "mem",
    "label": "MEM",
    "topic": "sys.mem",
    "aggregation": "avg",
    "window_s": 60,
    "unit": "%",
    "range": [0, 100],
    "style": "bar",
}
TEMP_GAUGE = {
    "id": "temp",
    "label": "TEMP",
    "topic": "sys.temp",
    "aggregation": "last",
    "window_s": 60,
    "unit": "C",
    "range": [0, 100],
    "style": "numeric",
}
DISK_GAUGE = {
    "id": "disk",
    "label": "DISK",
    "topic": "sys.disk:/",
    "aggregation": "max",
    "window_s": 60,
    "unit": "%",
    "range": [0, 100],
    "style": "sparkline",
}


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

    def __init__(self, base: Path, home: Path) -> None:
        self.base = base
        self.home = home
        self.runtime = base / "runtime"
        self.frames = base / "frames"
        self.clock = base / "clock"
        self.metrics = base / "metrics.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["SMART_PANEL_HOME"] = str(home)
        env["SMART_PANEL_RUNTIME"] = str(self.runtime)
        env["SMART_PANEL_TESTING"] = "1"
        env["SMART_PANEL_FAKE_PSUTIL"] = str(FAKE_PSUTIL)
        env["SMART_PANEL_FAKE_METRICS"] = str(self.metrics)
        env.pop("SMART_PANEL_JSON", None)
        env.pop("SMART_PANEL_RUNNER_UID", None)
        self.env = env

        self.home.mkdir(parents=True, exist_ok=True)
        self.write_metrics(cpu=25.0, mem=40.0, disk={"/": 61.0})
        (home / "config.json").write_text(
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
                                "x": MAIN[0],
                                "y": MAIN[1],
                                "w": MAIN[2],
                                "h": MAIN[3],
                                "instance": "gauges",
                            }
                        ],
                        "overlays": [],
                    },
                    "plugins": {
                        "sources": {
                            "plugin": "sources",
                            "enabled": True,
                            "settings": {
                                "sources": {
                                    "sys": {
                                        "type": "system",
                                        "interval_s": 2,
                                        # No ``temp``: the CI host has no sensor to
                                        # read, so the TEMP gauge stays on its
                                        # ``no data`` state on every platform.
                                        "settings": {"metrics": ["cpu", "mem", "disk:/"]},
                                    }
                                }
                            },
                        },
                        "gauges": {
                            "plugin": "gauges",
                            "enabled": True,
                            "settings": {
                                "gauges": [CPU_GAUGE, MEM_GAUGE, TEMP_GAUGE, DISK_GAUGE]
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        self.set_clock(0.0)

    # ------------------------------------------------------------- transport

    def write_metrics(self, **metrics: object) -> None:
        """Replace the fake psutil's metric file (a missing key fails its reader)."""
        self.metrics.write_text(json.dumps(metrics), encoding="utf-8")

    def cli(
        self, *args: str, input: str | None = None, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            input=input,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def ok(self, *args: str, input: str | None = None, timeout: float = 60.0) -> dict:
        proc = self.cli(*args, input=input, timeout=timeout)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, f"{args}: exit {proc.returncode}: {payload}"
        assert payload["ok"] is True, payload
        return payload["data"]

    def status(self) -> dict:
        return self.ok("--json", "status")

    def set_clock(self, value: float) -> None:
        self._clock_value = float(value)
        self.clock.write_text(f"{value}\n", encoding="utf-8")

    def tick(self, seconds: float = 2.0) -> float:
        """Advance the fake clock by one more ``system`` source interval."""
        self.set_clock(getattr(self, "_clock_value", 0.0) + float(seconds))
        return self._clock_value

    def start(self) -> dict:
        return self.ok(
            "--json",
            "--timeout",
            "30",
            "start",
            "--sink",
            "files",
            "--files-dir",
            str(self.frames),
            "--clock-file",
            str(self.clock),
            timeout=60,
        )

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "stop", timeout=30)

    def frame(self) -> PILImage.Image:
        with PILImage.open(self.frames / "latest.png") as image:
            image.load()
            return image.convert("RGB").copy()

    def wait_for_frame(
        self,
        predicate: Callable[[PILImage.Image], bool],
        *,
        timeout: float = 20.0,
        what: str = "the frame",
    ) -> PILImage.Image:
        deadline = time.monotonic() + timeout
        last: PILImage.Image | None = None
        while time.monotonic() < deadline:
            if (self.frames / "latest.png").exists():
                last = self.frame()
                if predicate(last):
                    return last
            time.sleep(0.05)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")

    def wait_for_status(
        self,
        predicate: Callable[[dict], bool],
        *,
        timeout: float = 20.0,
        what: str = "the status",
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.status()
            if predicate(last):
                return last
            time.sleep(0.2)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")


# --------------------------------------------------------------------- helpers


def _detail(status: dict) -> dict:
    return status["plugins"]["gauges"]["detail"]


def _gauge(status: dict, gauge_id: str) -> dict:
    for entry in _detail(status)["gauges"]:
        if entry["id"] == gauge_id:
            return entry
    raise AssertionError(f"no gauge named {gauge_id!r} in {_detail(status)}")


def _crop(image: PILImage.Image, box: tuple[int, int, int, int]) -> PILImage.Image:
    x, y, w, h = box
    return image.crop((x, y, x + w, y + h))


def _region(image: PILImage.Image, box: tuple[int, int, int, int]) -> bytes:
    return _crop(image, box).tobytes()


def _colours(image: PILImage.Image):
    return image.getcolors(maxcolors=1 << 20) or []


def _ink(image: PILImage.Image) -> int:
    colours = _colours(image)
    if not colours:
        return 0
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


def _count_colour(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    return sum(count for count, rgb in _colours(image) if rgb == colour)


def _cell_ink(image: PILImage.Image, columns: int, rows: int, index: int) -> int:
    main = _crop(image, MAIN)
    width, height = MAIN[2] // columns, MAIN[3] // rows
    column, row = index % columns, index // columns
    cell = main.crop((column * width, row * height, (column + 1) * width, (row + 1) * height))
    return _ink(cell)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="gauges-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home")
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------- the tests


def test_four_system_gauges_render_in_a_grid_with_thresholds_and_a_push(host: CliHost) -> None:
    host.start()

    def ready(status: dict) -> bool:
        return _detail(status)["count"] == 4 and _gauge(status, "cpu")["value"] is not None

    status = host.wait_for_status(ready, what="the first system metrics")
    assert [entry["id"] for entry in _detail(status)["gauges"]] == ["cpu", "mem", "temp", "disk"]
    assert _detail(status)["max"] == 6
    assert _gauge(status, "cpu")["value"] == pytest.approx(25.0)
    assert _gauge(status, "disk")["value"] == pytest.approx(61.0)
    assert _gauge(status, "temp")["freshness"] == "no_data"
    assert _gauge(status, "temp")["shown"] == "no data"

    # Four gauges, a 2x2 grid, every cell carrying content.
    first = host.wait_for_frame(lambda frame: _ink(_crop(frame, MAIN)) > 0, what="the gauges")
    assert first.size == CANVAS
    for index in range(4):
        assert _cell_ink(first, 2, 2, index) > 0, f"grid cell {index} is empty"

    # Crossing the critical threshold changes the colour (the CPU gauge is a dial
    # drawn in the threshold colour)...
    host.write_metrics(cpu=95.0, mem=40.0, disk={"/": 61.0})
    host.tick()
    host.wait_for_frame(
        lambda frame: _count_colour(_crop(frame, MAIN), CRIT) > 0, what="the critical colour"
    )
    host.wait_for_status(
        lambda status: _gauge(status, "cpu")["value"] == pytest.approx(95.0),
        what="the crossed threshold in status",
    )
    # ...the warning band for a value between the two thresholds...
    host.write_metrics(cpu=70.0, mem=40.0, disk={"/": 61.0})
    host.tick()
    host.wait_for_frame(
        lambda frame: _count_colour(_crop(frame, MAIN), WARN) > 0
        and _count_colour(_crop(frame, MAIN), CRIT) == 0,
        what="the warning colour",
    )
    # ...and it recovers when the value comes back down.
    host.write_metrics(cpu=10.0, mem=40.0, disk={"/": 61.0})
    host.tick()
    host.wait_for_frame(
        lambda frame: _count_colour(_crop(frame, MAIN), CRIT) == 0
        and _count_colour(_crop(frame, MAIN), WARN) == 0,
        what="the colour to recover",
    )

    # A pushed value reaches its gauge (Scenario E, FR-033/FR-035).
    added = host.ok(
        "--json",
        "gauge",
        "add",
        "--id",
        "q",
        "--label",
        "Queue",
        "--topic",
        "push.q",
        "--aggregation",
        "max",
        "--window",
        "300",
        "--style",
        "bar",
    )
    assert added["id"] == "q" and added["count"] == 5
    pushed = host.ok("--json", "gauge", "push", "q", "42")
    assert pushed["topic"] == "push.q" and pushed["value"] == 42
    host.wait_for_status(
        lambda status: _gauge(status, "q")["value"] == pytest.approx(42.0),
        what="the pushed value in status",
    )


def test_a_fifth_and_sixth_gauge_adapt_the_grid_and_a_seventh_is_rejected(host: CliHost) -> None:
    host.start()
    host.wait_for_status(
        lambda status: _gauge(status, "cpu")["value"] is not None, what="the first metrics"
    )
    before = host.wait_for_frame(lambda frame: _ink(_crop(frame, MAIN)) > 0, what="the gauges")

    for index, topic, style in ((5, "sys.mem", "numeric"), (6, "sys.disk:/", "bar")):
        added = host.ok(
            "--json",
            "gauge",
            "add",
            "--id",
            f"g{index}",
            "--label",
            f"G{index}",
            "--topic",
            topic,
            "--style",
            style,
        )
        assert added["count"] == index

    status = host.wait_for_status(
        lambda status: _detail(status)["count"] == 6, what="six gauges"
    )
    assert [entry["id"] for entry in _detail(status)["gauges"]] == [
        "cpu",
        "mem",
        "temp",
        "disk",
        "g5",
        "g6",
    ]

    # The grid adapts from 2x2 to 3x2; the fifth gauge lands in the first cell of
    # the new row...
    changed = host.wait_for_frame(
        lambda frame: _region(frame, MAIN) != _region(before, MAIN), what="the adapted grid"
    )
    assert _cell_ink(changed, 3, 2, 4) > 0, "the fifth gauge is not in the 3x2 grid"
    # ...and with the sixth every cell of the 3x2 grid carries content.
    complete = host.wait_for_frame(
        lambda frame: all(_cell_ink(frame, 3, 2, index) > 0 for index in range(6)),
        what="all six gauges",
    )
    assert complete.size == CANVAS

    # Scenario E: a seventh gauge is refused, naming the maximum of 6.
    proc = host.cli(
        "--json",
        "gauge",
        "add",
        "--id",
        "g7",
        "--label",
        "G7",
        "--topic",
        "sys.cpu",
        "--style",
        "dial",
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 2, payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"
    assert "6" in payload["error"]["message"], payload["error"]["message"]
    assert "maximum" in payload["error"]["message"].lower(), payload["error"]["message"]
    assert _detail(host.status())["count"] == 6


def test_a_source_that_stops_reporting_shows_the_stale_state(host: CliHost) -> None:
    host.start()
    host.wait_for_status(
        lambda status: _gauge(status, "cpu")["value"] is not None, what="the first metrics"
    )
    before = host.wait_for_frame(lambda frame: _ink(_crop(frame, MAIN)) > 0, what="the gauges")

    # The CPU reader stops answering: its topic keeps its last sample and ages out
    # (3x the observed 2 s cadence, which the gauge derives itself, FR-032/FR-036).
    host.write_metrics(mem=40.0, disk={"/": 61.0})
    host.set_clock(30.0)

    # A clock jump past the staleness limit makes every gauge show no data until
    # its source's next refresh, so wait for the joint state: the CPU gauge stale
    # because its metric is gone, the MEM gauge fresh again after its 2 s refresh.
    status = host.wait_for_status(
        lambda status: _gauge(status, "cpu")["stale"] is True
        and _gauge(status, "mem")["stale"] is False,
        what="the stale cpu gauge next to a fresh mem gauge",
    )
    assert _gauge(status, "cpu")["shown"] == "no data"
    assert _gauge(status, "cpu")["value"] is not None, (
        "the stale gauge keeps its last value for status, but does not show it"
    )

    stale_frame = host.wait_for_frame(
        lambda frame: _region(frame, MAIN) != _region(before, MAIN),
        what="the stale gauge to be redrawn",
    )
    assert stale_frame.convert("RGB").getpixel((5, 5)) == (0, 0, 0)  # outside the gauge region
    assert _crop(stale_frame, MAIN).convert("RGB").getpixel((2, 2)) == MAIN_BG
    assert host.status()["state"] == "running"
