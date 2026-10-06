"""Orientation transforms between the landscape canvas and the panel's native portrait buffer.

The physical panel is a wide bar (9.16"), but its native display buffer is
462×1920 (portrait). The vendor software presents a 1920×462 landscape canvas
to the user and rotates internally before encoding.

Open question: which direction (CW vs CCW) does the vendor software rotate?
This module provides both options — test empirically with the physical panel.
"""

from PIL import Image


def to_panel_native(
    landscape_img: Image.Image, clockwise: bool = True
) -> Image.Image:
    """Rotate a 1920×462 landscape canvas to the panel's native 462×1920 portrait.

    Args:
        landscape_img: PIL Image at 1920×462 (landscape).
        clockwise: If True, rotate 270° CCW (most likely correct).
                   If False, rotate 90° CW (fallback).

    Returns:
        462×1920 portrait PIL Image ready for JPEG encoding.
    """
    return landscape_img.transpose(
        Image.ROTATE_270 if clockwise else Image.ROTATE_90
    )
