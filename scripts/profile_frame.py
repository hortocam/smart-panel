#!/usr/bin/env python3
"""Profile one frame of the composed display (T119, SC-005).

Measures the per-frame cost of the two steps the panel path spends its time in:
``Compositor.composite()`` (paste the region layers and the alert overlay onto
the 1920x462 canvas) and ``Encoder.encode()`` (rotate, JPEG at the ladder's
current rung, packetise). It drives the *real* runner — the real plugin loader,
the real render workers, the real compositor and encoder — with the null sink, so
no panel is needed, and seeds the display the way the quickstart does:

* **six gauges** (the FR-031 maximum) with pushed values, so they draw numbers;
* **crawl** with 30 CLI items;
* **sidebar** with five entries (plus the implicit alert entry);
* **an alert shown** over the main region.

For every measured frame the script asks each render worker for a fresh image and
composes from those layers — the same sequence ``Runner._compose_once`` performs
before it publishes — then times ``composite()`` plus ``encode()`` together.
Reporting them separately would flatter the figure: the plan's budget ("frame
compose plus encode <= 40 ms p95 on a Pi 4B", SC-005) is for the pair.

The Pi half of T124 runs this same script on the real hardware; the numbers
recorded in ``research.md`` (decision 14) come from the development machine.

Usage::

    python scripts/profile_frame.py [--frames N] [--warmup N] [--json]

Run it from the repository root. The script prepends the repo root to
``sys.path``, so the package does not need to be installed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smart_panel.core.config import Config  # noqa: E402
from smart_panel.core.defaults import default_layout_for  # noqa: E402
from smart_panel.core.runner import Runner  # noqa: E402
from smart_panel.core.sinks import NullSink  # noqa: E402
from smart_panel.sdk.context import FrameContext  # noqa: E402

GAUGE_TOPICS = ("cpu", "mem", "temp", "disk", "queue", "errors")
SIDEBAR_ENTRIES = (
    ("mail", "mail", "Mail"),
    ("calendar", "calendar", "Cal"),
    ("chat", "chat", "Chat"),
    ("git", "git", "Git"),
    ("home", "home", "Home"),
)
CRAWL_ITEMS = 30


def build_config(home: Path) -> dict[str, Any]:
    """The config the profile runs against: all four built-ins, null sink."""
    instances = {
        "gauges": {"plugin": "gauges", "enabled": True, "settings": {}},
        "crawl": {"plugin": "crawl", "enabled": True, "settings": {}},
        "sidebar": {"plugin": "sidebar", "enabled": True, "settings": {}},
        "alerts": {"plugin": "alerts", "enabled": True, "settings": {}},
        "sources": {
            "plugin": "sources",
            "enabled": True,
            "settings": {"sources": {"sys": {"type": "pushed", "interval_s": 300}}},
        },
    }
    return {
        "version": 1,
        "display": {"sink": "null", "files_dir": None},
        "layout": default_layout_for(instances),
        "plugins": instances,
        "paths": {"spool": str(home / "spool")},
    }


def seed_display(runner: Runner) -> None:
    """Fill the display the way the quickstart does, through plugin verbs."""
    for index, topic in enumerate(GAUGE_TOPICS):
        runner.call_plugin(
            "gauges",
            "add",
            {
                "id": topic,
                "label": topic.upper(),
                "topic": f"push.{topic}",
                "aggregation": "last",
                "style": ("dial", "bar", "numeric", "sparkline")[index % 4],
                "unit": "%",
                "min": 0,
                "max": 100,
            },
        )
        runner.call_plugin("sources", "push", {"topic": f"push.{topic}", "value": 40 + index})
    runner.call_plugin(
        "crawl",
        "items set",
        {
            "items": [
                f"Headline {index}: a status line for the crawl" for index in range(CRAWL_ITEMS)
            ]
        },
    )
    for entry_id, icon, label in SIDEBAR_ENTRIES:
        runner.call_plugin(
            "sidebar",
            "entry add",
            {"id": entry_id, "icon": icon, "label": label, "topic": f"count.{entry_id}"},
        )
        runner.call_plugin("sidebar", "count set", {"id": entry_id, "value": 3})
    runner.call_plugin(
        "alerts",
        "push",
        {"severity": "critical", "title": "UPS", "message": "On battery", "id": "profile-1"},
    )


def wait_for(runner: Runner, predicate, timeout: float = 20.0) -> bool:
    """Poll ``predicate(status)`` until it is true or the deadline passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate(runner.status()):
            return True
        time.sleep(0.1)
    return False


def ready(status: dict[str, Any]) -> bool:
    """Whether every instance has rendered and the seeded content is present."""
    plugins = status.get("plugins") or {}
    states = {name: entry.get("state") for name, entry in plugins.items()}
    if not states or any(state not in ("ok", "degraded", "stalled") for state in states.values()):
        return False
    gauges = plugins["gauges"]["detail"]
    crawl = plugins["crawl"]["detail"]
    sidebar = plugins["sidebar"]["detail"]
    alerts = plugins["alerts"]["detail"]
    return (
        gauges.get("count") == len(GAUGE_TOPICS)
        and crawl.get("items") == CRAWL_ITEMS
        and sidebar.get("count") == len(SIDEBAR_ENTRIES)
        and alerts.get("current") is not None
    )


def compose_and_encode(runner: Runner, frame_index: int) -> tuple[float, float, int]:
    """Render every region freshly, then time ``composite()`` and ``encode()``.

    Returns:
        ``(compose_s, encode_s, jpeg_bytes)`` for one composed and encoded frame.
    """
    compositor = runner._compositor  # noqa: SLF001 - the runner's own compose path
    assert compositor is not None, "the runner is not started"
    config = runner.config_data
    base = FrameContext(
        size=compositor.size,
        now=time.monotonic(),
        wall=time.time(),
        frame_index=frame_index,
        fonts=runner._fonts,  # noqa: SLF001
        icons=runner._icons,  # noqa: SLF001
        bus=runner._bus,  # noqa: SLF001
        min_text_px=int(config.get("display", {}).get("min_text_px", 24)),
    )
    for name, worker in list(runner._workers.items()):  # noqa: SLF001
        worker.submit(runner._region_frame(base, worker))  # noqa: SLF001
        worker.wait_idle(timeout=2.0)
        compositor.set_state(name, worker.state)
        compositor.update(name, worker.last_image)
    started = time.perf_counter()
    canvas = compositor.composite()
    composed = time.perf_counter()
    runner._encoder.encode(canvas)  # noqa: SLF001
    encoded = time.perf_counter()
    return composed - started, encoded - composed, int(runner._encoder.last_jpeg_bytes or 0)  # noqa: SLF001


def percentile(samples: list[float], fraction: float) -> float:
    """The ``fraction`` percentile of ``samples`` (nearest-rank)."""
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), int(round(fraction * len(ordered) + 0.5))))
    return ordered[rank - 1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frames", type=int, default=120, help="frames to measure (default 120)")
    parser.add_argument("--warmup", type=int, default=10, help="frames to discard first")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(argv)

    base = Path(tempfile.mkdtemp(prefix="profile-frame-", dir="/tmp"))
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["SMART_PANEL_HOME"] = str(home)
    os.environ["SMART_PANEL_RUNTIME"] = str(base / "runtime")
    config = Config(path=home / "config.json")
    config.save(build_config(home))

    runner = Runner(config, sink=NullSink(), frame_interval_s=0.5)
    runner.start()
    try:
        if not wait_for(runner, lambda status: all(
            entry["state"] == "ok" for entry in (status.get("plugins") or {}).values()
        )):
            print("the built-in instances never reached 'ok'", file=sys.stderr)
            return 1
        seed_display(runner)
        if not wait_for(runner, ready):
            print("the seeded display was never ready", file=sys.stderr)
            return 1

        for index in range(max(0, args.warmup)):
            compose_and_encode(runner, index)

        seconds: list[float] = []
        compose_samples: list[float] = []
        encode_samples: list[float] = []
        sizes: list[int] = []
        for index in range(max(1, args.frames)):
            compose_s, encode_s, jpeg_bytes = compose_and_encode(runner, index)
            compose_samples.append(compose_s)
            encode_samples.append(encode_s)
            seconds.append(compose_s + encode_s)
            sizes.append(jpeg_bytes)

        report = {
            "frames": len(seconds),
            "gauge_count": len(GAUGE_TOPICS),
            "crawl_items": CRAWL_ITEMS,
            "sidebar_entries": len(SIDEBAR_ENTRIES),
            "compose_encode_ms": {
                "median": round(statistics.median(seconds) * 1000, 2),
                "p95": round(percentile(seconds, 0.95) * 1000, 2),
                "max": round(max(seconds) * 1000, 2),
            },
            "compose_ms": {
                "median": round(statistics.median(compose_samples) * 1000, 2),
                "p95": round(percentile(compose_samples, 0.95) * 1000, 2),
            },
            "encode_ms": {
                "median": round(statistics.median(encode_samples) * 1000, 2),
                "p95": round(percentile(encode_samples, 0.95) * 1000, 2),
            },
            "jpeg_bytes": {
                "median": int(statistics.median(sizes)),
                "p95": int(percentile([float(size) for size in sizes], 0.95)),
                "max": max(sizes),
            },
            "jpeg_quality": runner.status()["frames"]["quality"],
            "canvas": list(runner._compositor.size),  # noqa: SLF001
            "platform": sys.platform,
            "python": sys.version.split()[0],
        }
    finally:
        runner.stop()
        shutil.rmtree(base, ignore_errors=True)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        timing = report["compose_encode_ms"]
        sizes = report["jpeg_bytes"]
        print(f"frames: {report['frames']}  (gauges {report['gauge_count']}, "
              f"crawl {report['crawl_items']} items, sidebar {report['sidebar_entries']} entries, "
              "alert shown)")
        print(f"compose+encode: median {timing['median']} ms, p95 {timing['p95']} ms, "
              f"max {timing['max']} ms")
        print(f"  compose only: median {report['compose_ms']['median']} ms, "
              f"p95 {report['compose_ms']['p95']} ms")
        print(f"  encode only:  median {report['encode_ms']['median']} ms, "
              f"p95 {report['encode_ms']['p95']} ms")
        print(f"JPEG: median {sizes['median']} B, p95 {sizes['p95']} B, max {sizes['max']} B "
              f"(quality {report['jpeg_quality']})")
        print(f"host: {report['platform']} python {report['python']}, canvas {report['canvas']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
