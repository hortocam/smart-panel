"""End-to-end test: a drop-in plugin, with the file sink (T112, US6).

Runs quickstart **Scenario G** (the file-sink scenario) against the real CLI and
a real runner::

    cp examples/plugins/clock.py "$SMART_PANEL_HOME/plugins/"
    smart-panel plugin list                       # clock listed, no core changes
    smart-panel plugin instance add c1 --plugin clock
    smart-panel layout region set clock ... --instance c1
    smart-panel plugin call c1 crash
    smart-panel --json status                     # c1 degraded, others still render

It covers the T112 contract points:

* a sample plugin file copied into ``<home>/plugins/`` is discovered and listed;
* it is assigned to a region **by configuration alone** and renders;
* an invalid setting is rejected against the plugin's declared constraints;
* removing it leaves the runner unaffected;
* a name clash with a built-in, a bad name and a broken import are reported
  ``load_failed`` (and never stop the runner).
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

from smart_panel.core.compositor import ERROR_BADGE_DEGRADED

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_PLUGIN = REPO_ROOT / "examples" / "plugins" / "clock.py"
CANVAS = (1920, 462)
MAIN = (0, 0, 1152, 462)
CLOCK = (1152, 0, 768, 462)
DEGRADED_BADGE: tuple[int, int, int] = (
    ERROR_BADGE_DEGRADED[0],
    ERROR_BADGE_DEGRADED[1],
    ERROR_BADGE_DEGRADED[2],
)

# Drop-ins the loader must refuse, each for a different reason.
SHADOW_BUILTIN = """
from smart_panel.sdk.plugin import Manifest, Plugin

class Shadow(Plugin):
    manifest = Manifest(name="text", summary="shadows the built-in text")
"""

BAD_NAME = """
from smart_panel.sdk.plugin import Manifest, Plugin

class BadName(Plugin):
    manifest = Manifest(name="Bad Name", summary="not a legal plugin name")
"""

BROKEN_IMPORT = "import definitely_not_a_real_module_xyz  # noqa: F401\n"

REFUSED_DROPINS = {
    "shadow_text.py": SHADOW_BUILTIN,
    "badname.py": BAD_NAME,
    "broken.py": BROKEN_IMPORT,
}


class CliHost:
    """A CLI host pointed at a runner with a file sink and a fake clock."""

    def __init__(self, base: Path, home: Path, dropins: dict[str, str]) -> None:
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
        shutil.copyfile(SAMPLE_PLUGIN, plugins / "clock.py")  # Scenario G's `cp`
        for filename, source in dropins.items():
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
                            "settings": {"text": "MAIN", "fg": "#FFFFFF", "bg": "#000000"},
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

    def fail(self, *args: str, timeout: float = 60.0) -> tuple[int, dict]:
        proc = self.cli(*args, timeout=timeout)
        return proc.returncode, json.loads(proc.stdout)

    def status(self) -> dict:
        return self.ok("--json", "status")

    def set_clock(self, value: float) -> None:
        self.clock.write_text(f"{value}\n", encoding="utf-8")

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
        timeout: float = 25.0,
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
        timeout: float = 25.0,
        what: str = "the status",
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.status()
            if predicate(last):
                return last
            time.sleep(0.1)
        raise AssertionError(f"{what} never appeared within {timeout}s (last={last})")


# --------------------------------------------------------------------- helpers


def _crop(image: PILImage.Image, box: tuple[int, int, int, int]) -> PILImage.Image:
    x, y, w, h = box
    return image.crop((x, y, x + w, y + h))


def _colours(image: PILImage.Image) -> list:
    return image.getcolors(maxcolors=1 << 20) or []


def _ink(image: PILImage.Image) -> int:
    colours = _colours(image)
    if not colours:
        return 0
    background = max(colours, key=lambda pair: pair[0])[1]
    return sum(count for count, colour in colours if colour != background)


def _count_colour(image: PILImage.Image, colour: tuple[int, int, int]) -> int:
    return sum(count for count, rgb in _colours(image) if rgb == colour)


def _record(plugins: list[dict], name: str, *, source: str | None = None) -> dict:
    for record in plugins:
        if record["name"] == name and (source is None or record["source"] == source):
            return record
    raise AssertionError(f"no plugin record {name!r} (source={source}) in {plugins}")


@pytest.fixture
def host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="dropin-e2e-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", dropins={})
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def failing_host(tmp_path: Path) -> Iterator[CliHost]:
    base = Path(tempfile.mkdtemp(prefix="dropin-bad-", dir="/tmp"))
    cli_host = CliHost(base, tmp_path / "home", dropins=REFUSED_DROPINS)
    try:
        yield cli_host
    finally:
        cli_host.stop()
        shutil.rmtree(base, ignore_errors=True)


# ----------------------------------------------------------------- the tests


def test_a_dropin_plugin_is_listed_renders_crashes_and_removes(host: CliHost) -> None:
    host.start()
    host.wait_for_frame(lambda frame: _ink(_crop(frame, MAIN)) > 0, what="the text region")

    # Scenario G: the copied sample is discovered — with no core change.
    listed = host.ok("--json", "plugin", "list")["plugins"]
    record = _record(listed, "clock", source="dropin")
    assert record["state"] == "loaded"
    assert record["version"] == "1.0.0"

    # ``plugin describe`` prints the declared settings with types, ranges and
    # defaults (T115), and its commands.
    described = host.ok("--json", "plugin", "describe", "clock")
    settings = {setting["name"]: setting for setting in described["manifest"]["settings"]}
    assert settings["format"]["type"] == "str"
    assert settings["format"]["default"] == "%H:%M:%S"
    assert settings["size"]["type"] == "int"
    assert settings["size"]["min"] == 12 and settings["size"]["max"] == 512
    assert settings["fg"]["type"] == "color"
    assert {command["name"] for command in described["manifest"]["commands"]} >= {
        "crash",
        "reset",
    }

    # An invalid setting is refused from the plugin's declared constraints.
    code, payload = host.fail(
        "--json", "plugin", "instance", "add", "c1", "--plugin", "clock", "--set", "size=9999"
    )
    assert code == 2, payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_input"
    assert "512" in payload["error"]["message"]
    assert payload["error"]["setting"] == "plugins.c1.settings.size"
    # ...and an unknown setting is refused too.
    code, payload = host.fail(
        "--json", "plugin", "instance", "add", "c1", "--plugin", "clock", "--set", "nope=1"
    )
    assert code == 2 and payload["error"]["setting"] == "plugins.c1.settings.nope"

    # Assigned to a region by configuration alone.
    added = host.ok("--json", "plugin", "instance", "add", "c1", "--plugin", "clock")
    assert added["path"] == "plugins.c1"
    host.ok(
        "--json",
        "layout",
        "region",
        "set",
        "clock",
        "--x",
        str(CLOCK[0]),
        "--y",
        str(CLOCK[1]),
        "--w",
        str(CLOCK[2]),
        "--h",
        str(CLOCK[3]),
        "--instance",
        "c1",
    )
    status = host.wait_for_status(
        lambda status: "c1" in status["plugins"] and status["plugins"]["c1"]["state"] == "ok",
        what="the clock instance to render",
    )
    assert status["plugins"]["c1"]["detail"]["crashed"] is False
    clock_frame = host.wait_for_frame(
        lambda frame: _ink(_crop(frame, CLOCK)) > 0, what="the clock to draw"
    )
    assert clock_frame.size == CANVAS

    # Scenario G: ``c1 crash`` degrades only c1; the other region keeps rendering.
    crashed = host.ok("--json", "plugin", "call", "c1", "crash")
    assert crashed == {"crashed": True}
    status = host.wait_for_status(
        lambda status: status["plugins"]["c1"]["state"] == "degraded",
        what="the crashed instance",
    )
    assert "crash" in (status["plugins"]["c1"]["last_error"] or "")
    host.wait_for_frame(
        lambda frame: _ink(_crop(frame, MAIN)) > 0
        and _count_colour(_crop(frame, CLOCK), DEGRADED_BADGE) > 0,
        what="the crashed region's badge beside the healthy region",
    )

    # Removing the drop-in instance leaves the runner unaffected.
    host.ok("--json", "plugin", "instance", "remove", "c1")
    remaining = host.status()
    assert remaining["state"] == "running"
    assert "c1" not in remaining["plugins"]
    assert _ink(_crop(host.wait_for_frame(lambda f: _ink(_crop(f, MAIN)) > 0), MAIN)) > 0


def test_refused_dropins_are_reported_load_failed(failing_host: CliHost) -> None:
    failing_host.start()

    listed = failing_host.ok("--json", "plugin", "list")["plugins"]
    # The built-in ``text`` stays loaded; the drop-in that shadows it is refused.
    assert _record(listed, "text", source="builtin")["state"] == "loaded"
    shadow = _record(listed, "text", source="dropin")
    assert shadow["state"] == "load_failed"
    assert "shadow" in (shadow["error"] or "")

    bad_name = next(
        record for record in listed if record["path"] and "badname.py" in record["path"]
    )
    assert bad_name["state"] == "load_failed"

    broken = next(record for record in listed if record["path"] and "broken.py" in record["path"])
    assert broken["state"] == "load_failed"
    assert "definitely_not_a_real_module_xyz" in (broken["error"] or "")

    # The valid sample is still there, and the runner is alive.
    assert _record(listed, "clock", source="dropin")["state"] == "loaded"
    assert failing_host.status()["state"] == "running"
