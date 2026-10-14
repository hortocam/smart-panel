"""End-to-end alert scenarios on the file sink (T066; quickstart B and C).

Drives the real ``python -m smart_panel.cli.main`` against a runner with a
``--sink files`` and the test-only fake clock, exactly as the quickstart's
no-panel scenarios do, and reads the evidence back from ``frames/latest.png``.
The layout is three ``text`` regions (``sidebar``/``main``/``crawl``, each with
its own background colour) plus the ``alerts`` overlay on ``main``, so the
pixel checks can tell the overlay apart from the regions underneath:

* an alert covers **exactly** the ``main`` region: the sidebar and crawl areas
  are byte-identical to the frame before the alert; with nothing displayed the
  overlay draws nothing (``Inactive``) and ``main`` keeps showing its region's
  own background — the overlay's empty state, pinned by
  ``contracts/builtin-plugins.md``;
* a ``+N queued`` badge appears once alerts wait behind the displayed one;
* a timed-out alert is recorded in history and counted once in the missed
  indicator, which ``alert clear-missed`` returns to 0 (Scenario B);
* a push while the runner is down is spooled (``saved_for_later``) and replayed
  on start (Scenario C), and a spooled alert older than its own display time is
  recorded ``expired_in_queue`` and counted missed instead of flashing;
* a flood of 500 alerts is absorbed: the queue keeps to its cap, the overflow is
  logged one line per discarded alert, and frames keep flowing.
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

from smart_panel.core.control.spool import write_entry

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
SIDEBAR = (0, 0, 192, 462)  # x, y, w, h
MAIN = (192, 0, 1728, 382)
CRAWL = (192, 382, 1728, 80)
ALERT_CRITICAL = (214, 40, 57)  # #D62839
ALERT_INFO = (46, 125, 215)  # #2E7DD7
MAIN_BG = (0, 0, 85)  # #000055, the text region under the overlay
SIDEBAR_BG = (0, 68, 0)  # #004400
CRAWL_BG = (85, 0, 0)  # #550000
EPOCH = 1_700_000_000.0  # ``runner.FileClock.EPOCH``
QUEUE_CAP = 50


# -------------------------------------------------------------------- fixtures


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

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

        self.home.mkdir(parents=True, exist_ok=True)
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
                                "name": "sidebar",
                                "x": SIDEBAR[0],
                                "y": SIDEBAR[1],
                                "w": SIDEBAR[2],
                                "h": SIDEBAR[3],
                                "instance": "side",
                            },
                            {
                                "name": "main",
                                "x": MAIN[0],
                                "y": MAIN[1],
                                "w": MAIN[2],
                                "h": MAIN[3],
                                "instance": "text",
                            },
                            {
                                "name": "crawl",
                                "x": CRAWL[0],
                                "y": CRAWL[1],
                                "w": CRAWL[2],
                                "h": CRAWL[3],
                                "instance": "crawl_text",
                            },
                        ],
                        "overlays": [{"region": "main", "instance": "alerts", "z": 0}],
                    },
                    "plugins": {
                        "side": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "S", "bg": "#004400", "size": 120},
                        },
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "MAIN", "bg": "#000055", "size": 200},
                        },
                        "crawl_text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {"text": "C", "bg": "#550000", "size": 60},
                        },
                        "alerts": {"plugin": "alerts", "enabled": True, "settings": {}},
                    },
                }
            ),
            encoding="utf-8",
        )
        self.set_clock(0.0)

    # ------------------------------------------------------------- transport

    def cli(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def data(self, *args: str, timeout: float = 60.0) -> tuple[dict, int]:
        proc = self.cli(*args, timeout=timeout)
        return json.loads(proc.stdout), proc.returncode

    def ok(self, *args: str, timeout: float = 60.0) -> dict:
        payload, code = self.data(*args, timeout=timeout)
        assert code == 0, f"{args}: exit {code}: {payload}"
        assert payload["ok"] is True, payload
        return payload["data"]

    def set_clock(self, value: float) -> None:
        self.clock.write_text(f"{value}\n", encoding="utf-8")

    def start(self) -> dict:
        return self.ok(
            "--json",
            "--timeout",
            "15",
            "start",
            "--sink",
            "files",
            "--files-dir",
            str(self.frames),
            "--clock-file",
            str(self.clock),
            timeout=45,
        )

    def stop(self) -> subprocess.CompletedProcess[str]:
        return self.cli("--json", "stop", timeout=30)

    def frame(self) -> PILImage.Image:
        with PILImage.open(self.frames / "latest.png") as image:
            image.load()
            return image.convert("RGB").copy()

    def region(self, image: PILImage.Image, box: tuple[int, int, int, int]) -> bytes:
        x, y, w, h = box
        return image.crop((x, y, x + w, y + h)).tobytes()

    def wait_for_frame(
        self,
        predicate: Callable[[PILImage.Image], bool],
        *,
        timeout: float = 15.0,
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

    def log_events(self, event: str) -> list[dict]:
        """Every ``event`` in the runner log (JSON lines at ``<home>/runner.log``)."""
        path = self.home / "runner.log"
        if not path.exists():
            return []
        found = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") == event:
                found.append(record)
        return found


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="alerts-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home")
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


# ------------------------------------------------------------------ Scenario B


def test_an_alert_covers_exactly_the_main_region(host: CliHost) -> None:
    host.start()
    baseline = host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == MAIN_BG,
        what="the text region's own background on main (the overlay draws nothing)",
    )
    assert baseline.getpixel((10, 10)) == SIDEBAR_BG
    assert baseline.getpixel((1900, 400)) == CRAWL_BG

    pushed = host.ok(
        "--json",
        "alert",
        "push",
        "--severity",
        "critical",
        "--title",
        "UPS",
        "--message",
        "On battery",
        "--id",
        "ups-1",
        "--ttl",
        "60",
    )
    assert pushed["id"] == "ups-1" and pushed["state"] == "displayed"

    covered = host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == ALERT_CRITICAL,
        what="the alert covering the main region",
    )
    assert host.region(covered, SIDEBAR) == host.region(baseline, SIDEBAR), "sidebar changed"
    assert host.region(covered, CRAWL) == host.region(baseline, CRAWL), "crawl changed"
    assert covered.size == CANVAS

    # The alert clears itself when its time is up; the region underneath returns.
    host.set_clock(61.0)
    restored = host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == MAIN_BG,
        what="the main region after the alert timed out",
    )
    assert host.region(restored, SIDEBAR) == host.region(baseline, SIDEBAR)
    assert host.region(restored, CRAWL) == host.region(baseline, CRAWL)


def test_the_alert_entry_shows_a_badge_while_alerts_wait(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.ok(
        "--json", "alert", "push", "--severity", "critical", "--message", "shown", "--ttl", "600"
    )
    alone = host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == ALERT_CRITICAL,
        what="the displayed alert",
    )
    corner = (1620, 320, 300, 90)  # where the ``+N queued`` badge is drawn

    for index in range(3):
        host.ok("--json", "alert", "push", "--severity", "info", f"--message=queued {index}")
    assert len(host.ok("--json", "alert", "show")["queue"]) == 3

    badged = host.wait_for_frame(
        lambda frame: host.region(frame, corner) != host.region(alone, corner),
        what="the +N queued badge",
    )
    # The badge only adds a corner overlay: the alert body above it is unchanged.
    assert host.region(badged, (200, 0, 1000, 200)) == host.region(alone, (200, 0, 1000, 200))
    assert badged.getpixel((1900, 10)) == ALERT_CRITICAL


def test_a_short_alert_clears_itself_without_waiting_for_a_prune(host: CliHost) -> None:
    """A clock step below the 60 s prune cadence must still clear the alert.

    The retention prune runs every 60 s; the display countdown must run every
    frame, or a 2 s alert stays on the panel until the next retention pass.
    """
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.ok("--json", "alert", "push", "--severity", "info", "--message", "brief", "--ttl", "2")
    host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == ALERT_INFO,
        what="the brief alert",
    )
    host.set_clock(2.5)
    host.wait_for_frame(
        lambda frame: frame.getpixel((1900, 10)) == MAIN_BG,
        timeout=10.0,
        what="the main region after the brief alert timed out",
    )
    assert host.ok("--json", "alert", "show")["missed"] == {"count": 1, "max_severity": "info"}


def test_a_timed_out_alert_is_in_history_and_counted_once(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.ok(
        "--json", "alert", "push", "--severity", "critical", "--message", "on battery",
        "--id", "ups-1", "--ttl", "60",
    )

    host.set_clock(61.0)
    history = host.ok("--json", "alert", "list", "--state", "timed_out")["alerts"]
    assert [alert["id"] for alert in history] == ["ups-1"], history
    assert history[0]["outcome"] == "timed_out"

    show = host.ok("--json", "alert", "show")
    assert show["current"] is None
    assert show["missed"] == {"count": 1, "max_severity": "critical"}

    assert host.ok("--json", "alert", "clear-missed") == {"cleared": 1}
    assert host.ok("--json", "alert", "show")["missed"] == {"count": 0, "max_severity": None}

    # ``resolve`` works wherever the alert is, and is never counted as missed.
    assert host.ok("--json", "alert", "resolve", "--id", "ups-1")["outcome"] == "resolved"
    assert host.ok("--json", "alert", "show")["missed"]["count"] == 0


def test_a_bad_severity_exits_2_and_a_message_is_truncated(host: CliHost) -> None:
    host.start()
    payload, code = host.data(
        "--json", "alert", "push", "--severity", "bogus", "--message", "y"
    )
    assert code == 2, payload
    assert payload["error"]["code"] == "invalid_input"

    data = host.ok("--json", "alert", "push", "--severity", "info", "--message", "x" * 400)
    assert data["warning"] == "message truncated to 280 characters"


# ------------------------------------------------------------------ Scenario C


def test_a_push_while_the_runner_is_down_is_spooled_and_replayed(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.stop()

    data = host.ok("--json", "alert", "push", "--severity", "warning", "--message", "while down")
    assert data["saved_for_later"] is True
    assert data.get("spool_id"), data
    _, code = host.data("--json", "status")
    assert code == 3, "status must report runner_not_running while the push waits"

    clock_before = 0.0
    host.set_clock(clock_before)
    host.start()
    show = host.ok("--json", "alert", "show")
    assert show["current"] is not None, "the spooled alert was not replayed"
    assert show["current"]["message"] == "while down"
    assert show["current"]["state"] == "displayed"


def test_a_spooled_alert_older_than_its_duration_is_expired_and_missed(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.stop()

    pending = host.home / "spool-pending"
    pending.mkdir(parents=True, exist_ok=True)
    write_entry(
        pending,
        "plugin.call",
        {"instance": "alerts", "verb": "push", "args": {"severity": "info", "message": "too late"}},
        received_at=EPOCH - 600.0,
    )

    host.start()
    history = host.ok("--json", "alert", "list")["alerts"]
    assert [alert["state"] for alert in history] == ["expired_in_queue"], history
    show = host.ok("--json", "alert", "show")
    assert show["current"] is None
    assert show["missed"] == {"count": 1, "max_severity": "info"}


def test_a_flood_of_alerts_keeps_the_cap_logs_every_discard_and_keeps_frames(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: frame.getpixel((1900, 10)) == MAIN_BG)
    host.stop()

    pending = host.home / "spool-pending"
    pending.mkdir(parents=True, exist_ok=True)
    flood = 500
    for index in range(flood):
        write_entry(
            pending,
            "plugin.call",
            {
                "instance": "alerts",
                "verb": "push",
                "args": {"severity": "info", "message": f"flood {index}"},
            },
            received_at=time.time() + index * 1e-6,  # keep receive-time order
        )

    host.start()
    show = host.ok("--json", "alert", "show")
    assert len(show["queue"]) == QUEUE_CAP, "the queue must hold to its cap"
    displayed = 1
    dropped = flood - displayed - QUEUE_CAP
    assert show["missed"]["count"] == dropped
    discarded = host.log_events("alert_discarded")
    assert len(discarded) == dropped, f"{len(discarded)} discards logged, expected {dropped}"

    expired = host.ok("--json", "alert", "list", "--state", "expired_in_queue", "--limit", "500")
    assert len(expired["alerts"]) == dropped

    # Frames keep flowing after the flood (the render budget is not blown).
    before = (host.frames / "latest.png").stat().st_mtime_ns
    time.sleep(0.4)
    after = (host.frames / "latest.png").stat().st_mtime_ns
    assert after != before, "the runner stopped sending frames after the flood"
    assert host.ok("--json", "status")["state"] == "running"
