#!/usr/bin/env python3
"""Stream a landscape background image to the USB bar display.

Usage:
    python scripts/stream.py

Power-cycle the panel, then the script will retry opening the device
every second for 60 seconds and start streaming immediately.
"""

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


def main():
    # Load background image
    if not os.path.exists(BG_PATH):
        print(f"ERROR: Background image not found at {BG_PATH}")
        print("Run: curl -sLo assets/landscape_bg.jpg https://picsum.photos/1920/462")
        sys.exit(1)

    bg = Image.open(BG_PATH).convert("RGB")
    # Ensure it's exactly 1920x462
    if bg.size != (1920, 462):
        bg = bg.resize((1920, 462), Image.LANCZOS)
        print(f"Resized background to {bg.size}")

    # Pre-encode the frame
    portrait = to_panel_native(bg, clockwise=True)
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=70)
    packets = build_frame_packets(buf.getvalue())
    print(f"Background: {bg.size}, JPEG: {len(buf.getvalue())} bytes, {len(packets)} packets")

    # Wait for device
    print("Power-cycle the panel now. Retrying every second for 60s...")
    for attempt in range(60):
        try:
            dev = open_device()
            print(f"Device opened after {attempt+1}s!")
            break
        except Exception:
            if attempt % 5 == 0:
                print(f"  waiting... ({attempt+1}s)")
            time.sleep(1)
    else:
        print("Failed to open device after 60s")
        sys.exit(1)

    # Init and stream
    print("Sending init sequence (CRTDIS + CRTLIG)...")
    init_display(dev, brightness=50)
    print("Streaming at ~15 fps...")

    frame_count = 0
    t0 = time.time()
    try:
        while True:
            write_frame(dev, packets)
            frame_count += 1
            if frame_count % 300 == 0:
                elapsed = time.time() - t0
                fps = frame_count / elapsed
                print(f"  {frame_count} frames in {elapsed:.0f}s ({fps:.1f} fps)")
            time.sleep(1.0 / 15)
    except KeyboardInterrupt:
        print(f"\nStopped after {frame_count} frames in {time.time()-t0:.0f}s")
    finally:
        dev.close()
        print("Device closed.")


if __name__ == "__main__":
    main()
