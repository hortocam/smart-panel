"""Pin: ``text set --text ''`` is a valid blank frame (t_c47a1052).

Contract ``contracts/builtin-plugins.md`` (``text``) permits an empty ``text``
value and defines it as a background-only frame: ``set --text ""`` sets the
field to no text, the region keeps its size, and nothing is drawn. This is the
**only** way to blank a text region — the ``text`` plugin has no ``clear`` verb —
so forbidding empty text would remove capability rather than tighten validation,
and a clamp would silently substitute content the user did not ask for. (The
``alerts`` ``push`` verb still rejects an empty ``message``: a notification must
communicate something, a display field may legitimately carry nothing.)

The assertions are two-sided on purpose, so the test goes red on either rejected
reading: option (a) "refuse" makes ``handle_command`` raise (red before the
extrema check), and option (c) "clamp/substitute" leaves ink on the canvas, so
``(0, 0) != extrema`` fails. The blank-frame extrema ``min == max == 0`` is
observed through a real :class:`~smart_panel.sdk.testing.PluginHarness` render.
"""

from __future__ import annotations

from PIL import Image as PILImage

from smart_panel.plugins.text import TextPlugin
from smart_panel.sdk.testing import PluginHarness

CANVAS = (1920, 462)


def _luminance_extrema(image: PILImage.Image) -> tuple[int, int]:
    """The (min, max) luminance of a rendered frame; ``(0, 0)`` means all black."""
    return image.convert("L").getextrema()  # type: ignore[return-value]


def test_empty_text_is_accepted_and_draws_a_blank_frame() -> None:
    harness = PluginHarness(TextPlugin, settings={"text": "HELLO", "size": 120}, size=CANVAS)
    try:
        # Baseline: a non-empty text paints ink, so ``(0, 0)`` below is not vacuous.
        first = harness.render()
        assert first is not None
        assert _luminance_extrema(first) != (0, 0)

        # ``set --text ''`` is accepted and reports the empty value back.
        assert harness.command("set", text="") == {"text": ""}
        assert harness.state()["text"] == ""

        image = harness.render()
        assert image is not None
        assert image.size == CANVAS, "an empty text still renders the full region"
        assert _luminance_extrema(image) == (0, 0), (
            "an empty text must draw a blank frame (min == max == 0), "
            "not refuse and not clamp"
        )
    finally:
        harness.stop()
