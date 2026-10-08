#!/usr/bin/env python3
"""Test: open device immediately and stream, with retry on device not ready.

The panel's firmware has a timeout. This script retries the HID open
in a loop so it catches the device as soon as it's reconnected.
"""

import io
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import hid
from PIL import Image

from panel_driver.protocol import build_frame_packets
from panel_driver.rotation import to_panel_native


def main():
    # Pre-load everything
    print("Loading test pattern...")
    landscape = Image.open("assets/test_pattern_1920x462.png")
    portrait = to_panel_native(landscape, clockwise=True)
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=70)
    packets = build_frame_packets(buf.getvalue())
    print(f"Frame ready: {len(packets)} packets")

    print("\nPower-cycle the panel now. Will retry opening every second...")

    # Retry opening the device until it appears
    dev = None
    while dev is None:
        try:
            dev = hid.Device(0x5548, 0x1011)
            print("Device opened!")
        except hid.HIDException:
            print(".", end="", flush=True)
            time.sleep(1)

    # Start streaming immediately
    print("\nStreaming at ~15 fps...")
    frame_count = 0
    t0 = time.time()
    try:
        while True:
            for pkt in packets:
                dev.write(b"\x00" + pkt)
            frame_count += 1
            if frame_count % 300 == 0:
                elapsed = time.time() - t0
                fps = frame_count / elapsed
                print(f"  {frame_count} frames in {elapsed:.0f}s ({fps:.1f} fps)")
            time.sleep(1.0 / 15)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError at {frame_count} frames ({time.time()-t0:.0f}s): {e}")
    finally:
        if dev:
            dev.close()
        print(f"Stopped after {frame_count} frames in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
