"""Unit tests for ``smart_panel.core.encoder`` (T026).

A flat canvas fits at the start quality; a busier canvas forces the encoder
down the quality ladder; a canvas that cannot fit at the lowest rung raises
``FrameTooLarge``; and quality steps back up after a streak of small frames.

The noisy images are generated deterministically (fixed seeds); the sizes were
chosen with margin against the 65,503-byte JPEG limit so the assertions are not
tied to one libjpeg build.
"""

from __future__ import annotations

import random

import pytest
from PIL import Image

from smart_panel.core.encoder import MAX_JPEG_BYTES, Encoder, FrameTooLarge


def _flat() -> Image.Image:
    return Image.new("RGB", (1920, 462), (0, 0, 0))


def _blocky() -> Image.Image:
    """A blockwise-noisy canvas: overflows at the start quality, not at q40."""
    rng = random.Random(5)
    small = (120, 29)
    image = Image.new("RGB", small)
    image.putdata(
        [
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(small[0] * small[1])
        ]
    )
    return image.resize((1920, 462), Image.NEAREST)


def _noisy() -> Image.Image:
    """Per-pixel noise: cannot fit at any rung of the ladder."""
    rng = random.Random(11)
    return Image.frombytes("RGB", (1920, 462), rng.randbytes(1920 * 462 * 3))


def test_max_jpeg_bytes_is_the_frame_limit_minus_the_preamble() -> None:
    assert MAX_JPEG_BYTES == 65535 - 32


def test_flat_canvas_fits_at_the_start_quality() -> None:
    encoder = Encoder()
    packets = encoder.encode(_flat())
    assert encoder.last_quality == 70
    # A flat 1920x462 canvas encodes to exactly 14,547 bytes -> 15 packets.
    assert len(packets) == 15
    assert all(len(packet) == 1024 for packet in packets)


def test_noisy_canvas_is_stepped_down_the_ladder() -> None:
    encoder = Encoder()
    packets = encoder.encode(_blocky())
    assert packets
    assert encoder.last_quality < 70
    # The frame the encoder produced must actually fit.
    assert encoder.last_jpeg_bytes <= MAX_JPEG_BYTES


def test_impossible_canvas_raises_frame_too_large() -> None:
    encoder = Encoder()
    with pytest.raises(FrameTooLarge):
        encoder.encode(_noisy())
    assert encoder.last_quality is None


def test_quality_steps_back_up_after_a_streak_of_small_frames() -> None:
    encoder = Encoder(recover_streak=2)
    encoder.encode(_blocky())
    low = encoder.last_quality
    assert low is not None and low < 70
    # The first small frame fits at the current rung but does not step up yet.
    encoder.encode(_flat())
    assert encoder.last_quality == low
    # The streak is reached now; the step up applies to the *next* frame, which
    # climbs one rung (a higher quality) to the rung above ``low``.
    encoder.encode(_flat())
    assert encoder.last_quality == low
    encoder.encode(_flat())
    assert encoder.last_quality == low + 10


def test_encoder_start_quality_is_configurable() -> None:
    encoder = Encoder(start_quality=50, min_quality=30)
    encoder.encode(_flat())
    assert encoder.last_quality == 50
