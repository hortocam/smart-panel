"""Unit tests for the landscape -> panel-native orientation transform (T006).

The panel's native buffer is portrait 462x1920 even though the physical bar is
wide, so a 1920x462 canvas is rotated before JPEG encoding. ``clockwise=True``
(Rotate 270, i.e. 90 degrees clockwise) is the empirically confirmed direction
(handoff doc section 3, resolved). These tests pin the geometry so a regression
in the transform is caught without a panel attached.
"""

from __future__ import annotations

from PIL import Image

from panel_driver.rotation import to_panel_native

LANDSCAPE = (1920, 462)
PORTRAIT = (462, 1920)

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)


def _canvas_with_markers() -> Image.Image:
    """A 1920x462 canvas with four distinct corner pixels."""
    img = Image.new("RGB", LANDSCAPE, (0, 0, 0))
    img.putpixel((0, 0), RED)                                   # top-left
    img.putpixel((LANDSCAPE[0] - 1, 0), GREEN)                  # top-right
    img.putpixel((0, LANDSCAPE[1] - 1), BLUE)                   # bottom-left
    img.putpixel((LANDSCAPE[0] - 1, LANDSCAPE[1] - 1), YELLOW)  # bottom-right
    return img


def _locate(img: Image.Image, colour: tuple[int, int, int]) -> tuple[int, int]:
    """Return the (x, y) of the single pixel equal to ``colour``."""
    data = img.tobytes()
    width = img.size[0]
    for index in range(0, len(data), 3):
        if tuple(data[index : index + 3]) == colour:
            pixel = index // 3
            return (pixel % width, pixel // width)
    raise AssertionError(f"colour {colour} not found")


def test_dimensions_swap_landscape_to_portrait() -> None:
    img = Image.new("RGB", LANDSCAPE)
    out = to_panel_native(img, clockwise=True)
    assert out.size == PORTRAIT


def test_clockwise_defaults_to_true() -> None:
    assert to_panel_native(Image.new("RGB", LANDSCAPE)).size == PORTRAIT


def test_clockwise_moves_landscape_top_left_to_portrait_top_right() -> None:
    out = to_panel_native(_canvas_with_markers(), clockwise=True)
    # 90 degrees clockwise: the landscape top-left corner lands at the
    # portrait top-right (x = 461, y = 0).
    assert _locate(out, RED) == (PORTRAIT[0] - 1, 0)


def test_all_four_corners_map_as_a_90_degree_clockwise_rotation() -> None:
    out = to_panel_native(_canvas_with_markers(), clockwise=True)
    assert _locate(out, RED) == (461, 0)      # landscape TL -> portrait TR
    assert _locate(out, GREEN) == (461, 1919)  # landscape TR -> portrait BR
    assert _locate(out, BLUE) == (0, 0)        # landscape BL -> portrait TL
    assert _locate(out, YELLOW) == (0, 1919)   # landscape BR -> portrait BL


def test_counterclockwise_is_the_inverse_of_clockwise() -> None:
    img = _canvas_with_markers()
    rotated = to_panel_native(img, clockwise=True)
    back = to_panel_native(rotated, clockwise=False)
    assert back.size == LANDSCAPE
    assert back.tobytes() == img.tobytes()


def test_clockwise_false_maps_top_left_to_bottom_left() -> None:
    out = to_panel_native(_canvas_with_markers(), clockwise=False)
    # Rotate 90 (90 degrees counterclockwise): landscape TL -> portrait BL.
    assert _locate(out, RED) == (0, PORTRAIT[1] - 1)
