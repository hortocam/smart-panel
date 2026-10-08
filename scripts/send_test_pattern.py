#!/usr/bin/env python3
"""One-shot: send the known test pattern PNG to the panel and verify orientation.

Usage:
    python scripts/send_test_pattern.py                    # clockwise=True (270° CCW)
    python scripts/send_test_pattern.py --ccw              # clockwise=False (90° CW)
    python scripts/send_test_pattern.py --loop N           # send N frames at 15 fps
    python scripts/send_test_pattern.py --loop N --ccw    # both flags

The test pattern has clear corner labels so you can tell which way the panel
displays it. If the image appears mirrored or upside-down, flip the rotation
flag and re-run.
"""

import argparse
import io
import os
import sys
import time

# Ensure the project root is on sys.path so we can import panel_driver
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from panel_driver.device import open_device, write_frame
from panel_driver.protocol import build_frame_packets
from panel_driver.rotation import to_panel_native

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
TEST_PATTERN_PATH = os.path.join(ASSETS_DIR, "test_pattern_1920x462.png")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send test pattern to USB bar display")
    parser.add_argument(
        "--ccw",
        action="store_true",
        help="Use 90° CW rotation instead of default 270° CCW",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        help="Send N frames continuously (default: 1, one-shot)",
    )
    args = parser.parse_args()

    clockwise = not args.ccw  # --ccw means clockwise=False
    direction = "270° CCW" if clockwise else "90° CW"
    print(f"Rotation: {direction} (clockwise={clockwise})")

    # Load the test pattern
    if not os.path.exists(TEST_PATTERN_PATH):
        print(f"ERROR: Test pattern not found at {TEST_PATTERN_PATH}", file=sys.stderr)
        sys.exit(1)

    landscape = Image.open(TEST_PATTERN_PATH)
    print(f"Loaded test pattern: {landscape.size} (landscape)")

    # Rotate to panel native orientation
    portrait = to_panel_native(landscape, clockwise=clockwise)
    print(f"Rotated to: {portrait.size} (portrait)")

    # Encode as JPEG
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=70)
    jpeg_bytes = buf.getvalue()
    print(f"JPEG size: {len(jpeg_bytes)} bytes")

    # Build frame packets
    packets = build_frame_packets(jpeg_bytes)
    print(f"Frame: {len(packets)} packets of 1024 bytes")

    # Open device and send
    print("Opening HID device (0x5548:0x1011)...")
    dev = open_device()
    print("Device opened. Sending frame(s)...")

    try:
        if args.loop > 0:
            for i in range(args.loop):
                write_frame(dev, packets)
                print(f"  Frame {i + 1}/{args.loop} sent")
                time.sleep(1.0 / 15)
        else:
            write_frame(dev, packets)
            print("  Frame sent (one-shot)")
    finally:
        dev.close()
        print("Device closed.")

    print("\nDone. Look at the physical panel and check orientation.")
    print("If it's wrong, re-run with --ccw (or without it).")


if __name__ == "__main__":
    main()
