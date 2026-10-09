#!/usr/bin/env python3
"""Stream a landscape background image to the USB bar display.

Usage:
    python scripts/stream.py [--fps N] [--seconds N]

Keeps the panel fed at a steady rate. The panel's HID controller intermittently
stops accepting OUT reports (measured on the Raspberry Pi 4B: `HIDException:
Connection timed out` after anywhere from ~30 to ~600 frames, with no USB
disconnect, no power event, and independent of frame rate or init byte order).
The fix is to reopen the handle and resend the init sequence -- which recovers on
the first attempt every time. This script does that automatically and reports the
effective frame rate, so "stream for ten minutes" is actually achievable.

    --seconds N   stop after N seconds (default: run until Ctrl-C)
    --fps N       frames per second (default 15)
"""

import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from panel_driver.device import init_display, open_device, write_frame
from panel_driver.protocol import build_frame_packets
from panel_driver.rotation import to_panel_native

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
BG_PATH = os.path.join(ASSETS_DIR, "landscape_bg.jpg")

OPEN_RETRIES = 60          # seconds to wait for the device at start / after a stall
BRIGHTNESS = 50


def open_with_retry(seconds: int = OPEN_RETRIES):
    """Open the device, retrying once a second. Returns (dev, waited_seconds)."""
    for attempt in range(seconds):
        try:
            return open_device(), attempt
        except Exception:
            if attempt % 5 == 0:
                print(f"  waiting for device... ({attempt + 1}s)")
            time.sleep(1)
    return None, seconds


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream an image to the USB bar display")
    parser.add_argument("--fps", type=int, default=15, help="frames per second (default 15)")
    parser.add_argument("--seconds", type=int, default=0,
                        help="stop after N seconds (default: run until Ctrl-C)")
    args = parser.parse_args()

    if not os.path.exists(BG_PATH):
        print(f"ERROR: Background image not found at {BG_PATH}")
        print("Run: curl -sLo assets/landscape_bg.jpg https://picsum.photos/1920/462")
        sys.exit(1)

    bg = Image.open(BG_PATH).convert("RGB")
    if bg.size != (1920, 462):
        bg = bg.resize((1920, 462), Image.LANCZOS)
        print(f"Resized background to {bg.size}")

    portrait = to_panel_native(bg, clockwise=True)
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=70)
    packets = build_frame_packets(buf.getvalue())
    print(f"Background: {bg.size}, JPEG: {len(buf.getvalue())} bytes, {len(packets)} packets")

    print("Opening device (retrying every second for 60s)...")
    dev, waited = open_with_retry()
    if dev is None:
        print("Failed to open device after 60s")
        sys.exit(1)
    print(f"Device opened after {waited + 1}s; sending init sequence (CRTDIS + CRTLIG)...")
    init_display(dev, brightness=BRIGHTNESS)

    fps = max(1, args.fps)
    frame_count = 0
    stalls = 0
    t0 = time.time()
    deadline = t0 + args.seconds if args.seconds else None
    print(f"Streaming at ~{fps} fps"
          + (f" for {args.seconds}s..." if args.seconds else "..."))

    try:
        while deadline is None or time.time() < deadline:
            try:
                write_frame(dev, packets)
                frame_count += 1
            except Exception as e:
                # The panel dropped OUT reports. Reopen and resend the init sequence.
                stalls += 1
                elapsed = time.time() - t0
                print(f"  panel stopped accepting writes at frame {frame_count} "
                      f"({elapsed:.0f}s): {type(e).__name__}: {e}")
                try:
                    dev.close()
                except Exception:
                    pass
                dev, _ = open_with_retry()
                if dev is None:
                    print("Could not reopen the device after 60s; giving up")
                    break
                init_display(dev, brightness=BRIGHTNESS)
                print(f"  reopened (stall #{stalls}); resumed")
                continue

            if frame_count % 300 == 0:
                elapsed = time.time() - t0
                print(f"  {frame_count} frames in {elapsed:.0f}s "
                      f"({frame_count / elapsed:.1f} fps, {stalls} stalls)")
            time.sleep(1.0 / fps)
    except KeyboardInterrupt:
        print()
    finally:
        elapsed = time.time() - t0
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass
        summary = (f"\nStopped after {frame_count} frames in {elapsed:.0f}s "
                   f"({frame_count / elapsed:.1f} fps effective)")
        if stalls:
            summary += f", {stalls} stalls, all reopened"
        print(summary)
        print("Device closed.")


if __name__ == "__main__":
    main()
