#!/usr/bin/env python3
"""Benchmark raw panel throughput and sustainable frame rate.

**Hardware-only: this script needs the physical HOTSPOTEK bar display
(0x5548:0x1011) attached.** It cannot run on a machine without the panel —
there is no file or null sink here — so it is not part of the default test
suite and is not exercised in CI. Run it on the Raspberry Pi (or the dev Mac)
with the panel connected; the result feeds `handoff/panel_protocol_handoff.md`.

What it does: opens the panel, sends the bundled test pattern continuously for
N seconds with no inter-frame sleep, and reports:

- packets per second on the wire,
- per-frame write time (median and p95, in ms),
- sustainable frames per second (the achieved rate under back-to-back writes).

Usage:
    python scripts/bench_throughput.py                 # 10 s, quality 70
    python scripts/bench_throughput.py --seconds 30 --quality 60

Run from the repo root so `assets/test_pattern_1920x462.png` resolves.
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import time

# Ensure the project root is on sys.path so we can import panel_driver.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from panel_driver.device import init_display, open_device, write_frame
from panel_driver.protocol import build_frame_packets
from panel_driver.rotation import to_panel_native

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
TEST_PATTERN_PATH = os.path.join(ASSETS_DIR, "test_pattern_1920x462.png")


def _percentile(values: list[float], pct: float) -> float:
    """Return the `pct` percentile (0–100) of `values` by nearest-rank.

    No external dependency (the transport stays minimal); good enough for a
    benchmark, and defined for any sample count.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100.0 * len(ordered) + 0.5)))
    return ordered[rank - 1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark panel throughput (hardware only)"
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=10.0,
        help="How long to stream, in seconds (default: 10)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=70,
        help="JPEG quality for the test pattern (default: 70)",
    )
    args = parser.parse_args()

    if not os.path.exists(TEST_PATTERN_PATH):
        print(f"ERROR: test pattern not found at {TEST_PATTERN_PATH}", file=sys.stderr)
        sys.exit(1)

    landscape = Image.open(TEST_PATTERN_PATH)
    portrait = to_panel_native(landscape, clockwise=True)
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=args.quality)
    jpeg_bytes = buf.getvalue()
    packets = build_frame_packets(jpeg_bytes)
    packets_per_frame = len(packets)

    print(
        f"Test pattern: {landscape.size} -> {portrait.size}, "
        f"JPEG {len(jpeg_bytes)} bytes, {packets_per_frame} packets/frame, "
        f"quality {args.quality}"
    )

    print("Opening HID device (0x5548:0x1011)...")
    dev = open_device()
    frame_times_ms: list[float] = []
    total_packets = 0
    frames = 0
    try:
        init_display(dev, brightness=50)
        print(f"Streaming back-to-back for {args.seconds:g}s (no sleep)...")
        start = time.perf_counter()
        deadline = start + args.seconds
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            write_frame(dev, packets)
            frame_times_ms.append((time.perf_counter() - t0) * 1000.0)
            total_packets += packets_per_frame
            frames += 1
        elapsed = time.perf_counter() - start
    finally:
        dev.close()

    if frames == 0:
        print("ERROR: no frames were sent (elapsed too short?)", file=sys.stderr)
        sys.exit(1)

    fps = frames / elapsed
    pps = total_packets / elapsed
    median_ms = statistics.median(frame_times_ms)
    p95_ms = _percentile(frame_times_ms, 95.0)

    print("\n--- results ---")
    print(f"frames sent          : {frames}")
    print(f"elapsed              : {elapsed:.3f} s")
    print(f"sustainable fps      : {fps:.1f}")
    print(f"packets per second   : {pps:.1f}")
    print(f"packets per frame    : {packets_per_frame}")
    print(f"frame write time     : median {median_ms:.2f} ms, p95 {p95_ms:.2f} ms")
    print(f"per-packet (median)  : {median_ms / packets_per_frame:.3f} ms")
    print(
        "\nRecord this in handoff/panel_protocol_handoff.md "
        "(decision 1 in research.md)."
    )


if __name__ == "__main__":
    main()
