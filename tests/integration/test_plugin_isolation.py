"""End-to-end test: a misbehaving plugin affects only its own region (T111, US6).

Runs the isolation half of quickstart **Scenario G** against the real CLI and a
real runner with the file sink, a fake clock and **drop-in** test plugins:

* one plugin raises in ``render``;
* one returns an image of the wrong size;
* one returns the wrong type (not a ``RenderResult``);
* one ``sleep``s past the 2 s hard deadline (``check_deadlines`` marks it
  ``stalled`` once the fake clock crosses the deadline);

For each, the plugin's own region is reported ``degraded``/``stalled`` with a
``last_error`` in ``status`` and carries an error badge, while the healthy
``text`` region next to it keeps rendering, and the runner survives and can
remove the plugin (FR-045, SC-009).

The failure modes are *contract* violations, so the plugins are ordinary
drop-ins under ``<home>/plugins/`` — no core code knows they exist.
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

from smart_panel.core.compositor import ERROR_BADGE_DEGRADED, ERROR_BADGE_STALLED

REPO_ROOT = Path(__file__).resolve().parents[2]
CANVAS = (1920, 462)
MAIN = (0, 0, 1152, 462)
BOOM = (1152, 0, 192, 154)
WRONG_SIZE = (1344, 0, 192, 154)
WRONG_TYPE = (1536, 0, 192, 154)
SLEEPER = (1728, 0, 192, 154)
DEGRADED_BADGE: tuple[int, int, int] = (
    ERROR_BADGE_DEGRADED[0],
    ERROR_BADGE_DEGRADED[1],
    ERROR_BADGE_DEGRADED[2],
)
STALLED_BADGE: tuple[int, int, int] = (
    ERROR_BADGE_STALLED[0],
    ERROR_BADGE_STALLED[1],
    ERROR_BADGE_STALLED[2],
)

# (instance name, plugin name, region name, box) for the four bad plugins.
BAD_PLUGINS = (
    ("boom", BOOM),
    ("wrongsize", WRONG_SIZE),
    ("wrongtype", WRONG_TYPE),
    ("sleeper", SLEEPER),
)
DEGRADED_INSTANCES = ("boom", "wrongsize", "wrongtype")

# Drop-in sources. Each declares the ``region`` capability and its own name.
BOOM_SOURCE = """
from smart_panel.sdk.plugin import Manifest, Plugin

class Boom(Plugin):
    manifest = Manifest(name="boom", summary="raises in render",
                        capabilities=frozenset({"region"}))

    def render(self, frame):
        raise RuntimeError("boom: render failed on purpose")
"""

WRONG_SIZE_SOURCE = """
from PIL import Image as PILImage
from smart_panel.sdk.plugin import Manifest, Plugin
from smart_panel.sdk.plugin import Image as RenderImage

class WrongSize(Plugin):
    manifest = Manifest(name="wrongsize", summary="returns the wrong size",
                        capabilities=frozenset({"region"}))

    def render(self, frame):
        return RenderImage(PILImage.new("RGBA", (7, 5), (0, 0, 0, 255)))
"""

WRONG_TYPE_SOURCE = """
from smart_panel.sdk.plugin import Manifest, Plugin

class WrongType(Plugin):
    manifest = Manifest(name="wrongtype", summary="returns the wrong type",
                        capabilities=frozenset({"region"}))

    def render(self, frame):
        return "not a RenderResult"
"""

SLEEPER_SOURCE = """
import time
from PIL import Image as PILImage
from smart_panel.sdk.plugin import Manifest, Plugin
from smart_panel.sdk.plugin import Image as RenderImage

class Sleeper(Plugin):
    manifest = Manifest(name="sleeper", summary="sleeps past the hard deadline",
                        capabilities=frozenset({"region"}))

    def render(self, frame):
        # Long enough that the instance is still in flight when the fake clock
        # crosses the 2 s hard deadline; the runner must abandon the thread.
        time.sleep(30.0)
        return RenderImage(PILImage.new("RGBA", tuple(frame.size), (0, 0, 0, 255)))
"""

DROPINS = {
    "boom.py": BOOM_SOURCE,
    "wrongsize.py": WRONG_SIZE_SOURCE,
    "wrongtype.py": WRONG_TYPE_SOURCE,
    "sleeper.py": SLEEPER_SOURCE,
}


# Every wait here polls a fresh CLI subprocess (``status``) or the frames file,
# and each sample costs seconds on a starved host — 25 s was not enough: a render
# worker left unscheduled on macos-latest (run 38521533126) had still not started
# its first render when the stalled-instance wait expired. 60 s is the bound the
# sibling e2e hosts already pair with their 30 s CLI ``start`` wait (their
# subprocess waits are ``--timeout`` + 30; see 4ea0909 and PR #54), and it covers
# both halves of this file's shape: the starved effect *and* the polling round
# trips. It stays bounded: a runner that never reaches the state still fails.
EFFECT_WAIT_S = 60.0


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
        plugins = home / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        for filename, source in DROPINS.items():
            (plugins / filename).write_text(source, encoding="utf-8")
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
                                "instance": "text",
                            }
                        ],
                        "overlays": [],
                    },
                    "plugins": {
                        "text": {
                            "plugin": "text",
                            "enabled": True,
                            "settings": {
                                "text": "HEALTHY",
                                "fg": "#FFFFFF",
                                "bg": "#000000",
                                "size": 64,
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.set_clock(0.0)

    # ------------------------------------------------------------- transport

    def cli(
        self, *args: str, timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "smart_panel.cli.main", *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )

    def ok(self, *args: str, timeout: float = 60.0) -> dict:
        proc = self.cli(*args, timeout=timeout)
        payload = json.loads(proc.stdout)
        assert proc.returncode == 0, f"{args}: exit {proc.returncode}: {payload}"
        assert payload["ok"] is True, payload
        return payload["data"]

    def status(self) -> dict:
        return self.ok("--json", "status")

    def set_clock(self, value: float) -> None:
        self._clock_s = float(value)
        self.clock.write_text(f"{value}\n", encoding="utf-8")

    def tick_clock(self, seconds: float = 5.0) -> None:
        """Advance the fake clock the runner reads (used while polling a step).

        A worker's hard deadline is measured from when its render *started* — a
        fake-clock reading the test cannot observe from outside — so a single
        ``set_clock`` step can miss it: a render that begins after the step has
        its whole deadline ahead of a frozen clock and is never marked stalled.
        Marching the clock while polling crosses the deadline whenever the render
        actually starts.
        """
        self.set_clock(self._clock_s + float(seconds))

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

    def add_plugin(self, instance: str, box: tuple[int, int, int, int]) -> None:
        """Add a drop-in instance and give it a region (configuration only)."""
        added = self.ok(
            "--json", "plugin", "instance", "add", instance, "--plugin", instance
        )
        assert added["path"] == f"plugins.{instance}"
        x, y, w, h = box
        self.ok(
            "--json",
            "layout",
            "region",
            "set",
            instance,
            "--x",
            str(x),
            "--y",
            str(y),
            "--w",
            str(w),
            "--h",
            str(h),
            "--instance",
            instance,
        )

    def frame(self) -> PILImage.Image:
        with PILImage.open(self.frames / "latest.png") as image:
            image.load()
            return image.convert("RGB").copy()

    def wait_for_frame(
        self,
        predicate: Callable[[PILImage.Image], bool],
        *,
        timeout: float = EFFECT_WAIT_S,
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
        timeout: float = EFFECT_WAIT_S,
        what: str = "the status",
        on_poll: Callable[[], None] | None = None,
    ) -> dict:
        """Poll ``runner.status`` until ``predicate`` accepts it.

        ``on_poll`` runs before each read, so a wait whose effect is a fake-clock
        step can drive that clock while it polls (see the stalled-instance wait):
        each read is a fresh CLI subprocess, so a starved host costs seconds per
        sample and the *effect* must not also be left to chance.
        """
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            if on_poll is not None:
                on_poll()
            last = self.status()
            if predicate(last):
                return last
            time.sleep(0.1)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")


# --------------------------------------------------------------------- helpers


def _crop(image: PILImage.Image, box: tuple[int, int, int, int]) -> PILImage.Image:
    x, y, w, h = box
    return image.crop((x, y, x + w, y + h))


def _colours(image: PILImage.Image) -> list[tuple[int, tuple[int, int, int]]]:
    return image.getcolors(maxcolors=1 << 20) or []


def _ink(image: PILImage.Image) -> int:
    colours = _colours(image)
    if not colours:
        return 0
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


def _count_colour(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    return sum(count for count, rgb in _colours(image) if rgb == colour)


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="isolation-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home")
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------- the test


def test_a_failing_plugin_affects_only_its_own_region(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: _ink(_crop(frame, MAIN)) > 0, what="the text region")

    for instance, _box in BAD_PLUGINS:
        host.add_plugin(instance, _box)

    # 1. The three contract violations are reported ``degraded`` with a message.
    status = host.wait_for_status(
        lambda status: all(
            status["plugins"][name]["state"] == "degraded" for name in DEGRADED_INSTANCES
        ),
        what="the degraded instances",
    )
    for name in DEGRADED_INSTANCES:
        assert status["plugins"][name]["last_error"], f"{name} reported no last_error"
    assert "invalid output size" in status["plugins"]["wrongsize"]["last_error"]
    assert "RenderResult" in status["plugins"]["wrongtype"]["last_error"]
    assert "on purpose" in status["plugins"]["boom"]["last_error"]

    # 2. The slow render crosses the 2 s hard deadline: ``stalled`` with the error,
    #    and the runner is still alive (it did not wait for the stuck thread).
    #    ``check_deadlines`` measures from when the render *started* — a fake-clock
    #    reading the test cannot see from outside, and one a starved runner may
    #    not reach until well after this step. So march the fake clock while
    #    polling (``tick_clock``) instead of stepping it once: a single step that
    #    lands before the render starts would leave the deadline behind a frozen
    #    clock, and the instance would never be marked stalled at all. The wait
    #    stays bounded, so a runner that never renders still fails.
    host.set_clock(5.0)
    status = host.wait_for_status(
        lambda status: status["plugins"]["sleeper"]["state"] == "stalled",
        what="the stalled instance",
        on_poll=host.tick_clock,
    )
    assert "hard deadline" in (status["plugins"]["sleeper"]["last_error"] or "")
    assert status["state"] == "running"

    # 3. Every other region keeps rendering, and each failing region carries an
    #    error badge (amber for degraded, red for stalled) while the healthy
    #    region carries none.
    def badged(frame: PILImage.Image) -> bool:
        if _ink(_crop(frame, MAIN)) == 0:
            return False
        if _count_colour(_crop(frame, MAIN), DEGRADED_BADGE):
            return False
        if _count_colour(_crop(frame, MAIN), STALLED_BADGE):
            return False
        return all(
            _count_colour(_crop(frame, box), DEGRADED_BADGE) > 0 for _name, box in
            (("boom", BOOM), ("wrongsize", WRONG_SIZE), ("wrongtype", WRONG_TYPE))
        ) and _count_colour(_crop(frame, SLEEPER), STALLED_BADGE) > 0

    frame = host.wait_for_frame(badged, what="the error badges beside the healthy region")
    assert frame.size == CANVAS
    assert _ink(_crop(frame, MAIN)) > 0

    # 4. The runner survives and can remove every misbehaving plugin.
    for instance, _box in BAD_PLUGINS:
        host.ok("--json", "plugin", "instance", "remove", instance)
    remaining = host.status()
    assert remaining["state"] == "running"
    assert all(name not in remaining["plugins"] for name, _box in BAD_PLUGINS)
    # The healthy region is untouched by any of it.
    after = host.wait_for_frame(
        lambda image: _ink(_crop(image, MAIN)) > 0, what="the healthy region after removal"
    )
    assert _ink(_crop(after, MAIN)) > 0
