#!/usr/bin/env python3
"""Stream test pattern continuously to verify sustained display.

Usage:
    python scripts/stream_test.py

The script waits for you to press Enter, then opens the device and streams.
Power-cycle the panel just before pressing Enter.
"""

import sys
import os
import io
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image
from panel_driver.device import open_device, write_frame
from panel_driver.protocol import build_frame_packets
from panel_driver.rotation import to_panel_native


def main():
    # Pre-load everything so there's no delay
    print("Loading test pattern...")
    landscape = Image.open("assets/test_pattern_1920x462.png")
    portrait = to_panel_native(landscape, clockwise=True)
    buf = io.BytesIO()
    portrait.save(buf, format="JPEG", quality=70)
    packets = build_frame_packets(buf.getvalue())
    print(f"Frame ready: {len(packets)} packets")

    input("Power-cycle the panel now, then press Enter to start streaming...")

    print("Opening device...")
    dev = open_device()
    print("Device opened. Streaming at ~15 fps...")

    frame_count = 0
    t0 = time.time()
    try:
        while True:
            write_frame(dev, packets)
            frame_count += 1
            if frame_count % 150 == 0:
                elapsed = time.time() - t0
                fps = frame_count / elapsed
                print(f"  {frame_count} frames in {elapsed:.0f}s ({fps:.1f} fps)")
            time.sleep(1.0 / 15)
    except KeyboardInterrupt:
        print(f"\nStopped after {frame_count} frames in {time.time()-t0:.0f}s")
    except Exception as e:
        print(f"\nError after {frame_count} frames: {e}")
    finally:
        dev.close()
        print("Device closed.")


if __name__ == "__main__":
    main()
