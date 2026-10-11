"""Unit tests for the file and null sinks in ``smart_panel.core.sinks`` (T027).

The file sink is the no-panel target used by the quickstart and the
integration tests: it writes the composed canvas to ``latest.png`` atomically
(so a reader never sees a half-written frame) and keeps numbered frames when
asked. ``HidSink`` is T028 and lives in the same module but is out of this
card's scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from smart_panel.core.sinks import (
    FileSink,
    NullSink,
    PanelSink,
    SinkState,
)


def _canvas(color: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (1920, 462), color)


def test_file_and_null_sinks_satisfy_the_panel_sink_protocol(tmp_path: Path) -> None:
    assert isinstance(FileSink(tmp_path / "frames"), PanelSink)
    assert isinstance(NullSink(), PanelSink)


def test_null_sink_discards_frames_and_stays_connected() -> None:
    sink = NullSink()
    sink.open()
    sink.set_brightness(10)
    sink.send(_canvas((0, 0, 0)))
    status = sink.state()
    assert status.state == SinkState.CONNECTED
    assert status.last_error is None
    sink.close()
    assert sink.state().state == SinkState.CONNECTED


def test_file_sink_writes_latest_png(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    sink = FileSink(frames)
    sink.open()
    sink.send(_canvas((10, 20, 30)))
    latest = frames / "latest.png"
    assert latest.exists()
    with Image.open(latest) as image:
        assert image.size == (1920, 462)
        assert image.convert("RGB").getpixel((0, 0)) == (10, 20, 30)


def test_file_sink_overwrites_latest_and_leaves_no_temp_files(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    sink = FileSink(frames)
    sink.open()
    sink.send(_canvas((10, 20, 30)))
    sink.send(_canvas((200, 0, 0)))
    with Image.open(frames / "latest.png") as image:
        assert image.convert("RGB").getpixel((0, 0)) == (200, 0, 0)
    assert list(frames.glob("*.tmp")) == []


def test_file_sink_keeps_the_previous_frame_on_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = tmp_path / "frames"
    sink = FileSink(frames)
    sink.open()
    sink.send(_canvas((10, 20, 30)))

    def boom(self: Image.Image, fp: object, *args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", boom)
    with pytest.raises(OSError):
        sink.send(_canvas((200, 0, 0)))

    # The previous frame is intact and no partial temp file is left behind.
    with Image.open(frames / "latest.png") as image:
        assert image.convert("RGB").getpixel((0, 0)) == (10, 20, 30)
    assert list(frames.glob("*.tmp")) == []


def test_file_sink_writes_numbered_frames_when_asked(tmp_path: Path) -> None:
    frames = tmp_path / "frames"
    sink = FileSink(frames, keep_frames=True)
    sink.open()
    for color in ((1, 0, 0), (2, 0, 0), (3, 0, 0)):
        sink.send(_canvas(color))
    names = sorted(path.name for path in frames.glob("frame-*.png"))
    assert names == ["frame-000001.png", "frame-000002.png", "frame-000003.png"]


def test_file_sink_records_the_frame_count(tmp_path: Path) -> None:
    sink = FileSink(tmp_path / "frames")
    sink.open()
    sink.send(_canvas((0, 0, 0)))
    sink.send(_canvas((0, 0, 0)))
    assert sink.state().frames == 2
