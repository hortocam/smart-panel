"""Continuous refresh loop: render → encode → frame → HID write.

The vendor software continuously re-sends frames even for static content
(video-style push, not "set once and forget"). This loop replicates that
behaviour at a configurable FPS.

Init sequence (CRTDIS + CRTLIG) is sent once before the first frame.
"""

import io
import time
from typing import Callable, Optional

from PIL import Image

from .device import open_device, init_display, write_frame
from .protocol import build_frame_packets
from .rotation import to_panel_native


def run(
    render_fn: Callable[[], Image.Image],
    fps: float = 15,
    clockwise: bool = True,
    jpeg_quality: int = 70,
    brightness: int = 50,
    max_frames: Optional[int] = None,
) -> None:
    """Continuously render and push frames to the panel.

    Args:
        render_fn: Callable that returns a 1920×462 PIL Image (landscape canvas).
        fps: Target frames per second.
        clockwise: Rotation direction (True = 270° CCW, False = 90° CW).
        jpeg_quality: JPEG encoding quality (1–100).
        brightness: Backlight brightness (0–100).
        max_frames: If set, stop after this many frames (for testing).
    """
    dev = open_device()
    interval = 1.0 / fps
    frame_count = 0

    try:
        # Send init sequence before first frame
        init_display(dev, brightness)

        while True:
            t0 = time.time()

            frame_img = render_fn()
            rotated = to_panel_native(frame_img, clockwise)

            buf = io.BytesIO()
            rotated.save(buf, format="JPEG", quality=jpeg_quality)
            packets = build_frame_packets(buf.getvalue())

            write_frame(dev, packets)

            frame_count += 1
            if max_frames is not None and frame_count >= max_frames:
                break

            elapsed = time.time() - t0
            sleep_time = interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    finally:
        dev.close()
