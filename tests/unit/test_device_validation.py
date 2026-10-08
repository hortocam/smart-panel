"""T007 - device/protocol validation hardening.

Safety net for the highest-consequence module in Phase 1: the code that writes to
the physical panel.

These tests run with **no hidapi library and no panel attached** (constitution
Principle IV). They pin three contracts:

1. Importing :mod:`panel_driver.device` must not require hidapi. The ``import
   hid`` lives inside :func:`~panel_driver.device.open_device`, so the pure
   protocol code can be imported and exercised anywhere, including CI runners
   without libhidapi installed.
2. Brightness is validated as an ``int`` in ``0..100`` at two levels: the
   ``device`` write functions and the ``protocol`` packet builders. The boundary
   is deliberately enforced twice (constitution Principle II) so a regression in
   one layer cannot let a malformed value reach the wire.
3. Every ``dev.write`` carries the hidapi report-ID prefix (``b"\\x00"``) and is
   exactly 1025 bytes (one report-ID byte + one 1024-byte packet), and only
   ``panel_driver/device.py`` calls ``dev.write``.

The :class:`FakeDevice` below records every write so the byte-level contract is
checked directly rather than assumed.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

import panel_driver
from panel_driver import device, protocol


class FakeDevice:
    """Stand-in for ``hid.Device`` that records every ``write()`` call."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def close(self) -> None:  # pragma: no cover - parity with hid.Device
        self.closed = True


def _assert_write_contract(writes: list[bytes]) -> None:
    """Every recorded write is ``b"\\x00"`` + one 1024-byte packet = 1025 bytes."""
    assert writes, "expected at least one dev.write"
    for write in writes:
        assert len(write) == 1025, f"write must be 1025 bytes, got {len(write)}"
        assert write[0:1] == b"\x00", "write must start with the 0x00 report-ID prefix"
        assert len(write[1:]) == 1024, "payload after the prefix must be 1024 bytes"


# ---------------------------------------------------------------------------
# 1. Importing the transport must not need hidapi
# ---------------------------------------------------------------------------


def test_device_imports_without_hidapi(monkeypatch: pytest.MonkeyPatch) -> None:
    # Poison ``hid`` so a top-level ``import hid`` would raise, then re-execute
    # the module body. It must complete cleanly because the import lives inside
    # open_device().
    monkeypatch.setitem(sys.modules, "hid", None)
    try:
        importlib.reload(device)
    except ImportError as exc:
        pytest.fail(
            "panel_driver.device must import with no hidapi installed; "
            f"move `import hid` inside open_device(): {exc}"
        )


# ---------------------------------------------------------------------------
# 2. Every dev.write keeps the report-ID prefix and is exactly 1025 bytes
# ---------------------------------------------------------------------------


def test_init_display_writes_two_1025_byte_packets() -> None:
    dev = FakeDevice()
    device.init_display(dev, brightness=50)
    assert len(dev.writes) == 2  # CRTDIS + CRTLIG
    _assert_write_contract(dev.writes)


def test_set_backlight_write_is_a_1025_byte_packet() -> None:
    dev = FakeDevice()
    device.set_backlight(dev, brightness=42)
    _assert_write_contract(dev.writes)


def test_write_frame_writes_1025_byte_packets() -> None:
    dev = FakeDevice()
    packets = protocol.build_frame_packets(
        b"\xff\xd8\xff\xe0" + b"\x11" * 5000 + b"\xff\xd9"
    )
    device.write_frame(dev, packets)
    _assert_write_contract(dev.writes)


def test_dev_write_calls_stay_inside_device_module() -> None:
    # Constitution II / engineer overlay: every dev.write lives in device.py.
    pkg_dir = Path(panel_driver.__file__).resolve().parent
    pattern = re.compile(r"\.write\(")
    offenders = sorted(
        path.name
        for path in pkg_dir.glob("*.py")
        if path.name != "device.py" and pattern.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == [], f"dev.write must stay inside device.py, found in {offenders}"


# ---------------------------------------------------------------------------
# 3. Brightness validation - device layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, 101, 255, 1000, -100])
def test_set_backlight_rejects_out_of_range(value: int) -> None:
    dev = FakeDevice()
    with pytest.raises(ValueError):
        device.set_backlight(dev, value)
    assert dev.writes == []  # nothing reached the wire


@pytest.mark.parametrize("value", [-1, 101, 255, 1000])
def test_init_display_rejects_out_of_range(value: int) -> None:
    dev = FakeDevice()
    with pytest.raises(ValueError):
        device.init_display(dev, value)
    assert dev.writes == []


@pytest.mark.parametrize("value", ["50", 50.0, None, True, b"50"])
def test_device_rejects_non_int_brightness(value: object) -> None:
    dev = FakeDevice()
    with pytest.raises(ValueError):
        device.set_backlight(dev, value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        device.init_display(dev, value)  # type: ignore[arg-type]
    assert dev.writes == []


@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_device_accepts_boundary_brightness(value: int) -> None:
    dev = FakeDevice()
    device.set_backlight(dev, value)
    device.init_display(dev, value)
    _assert_write_contract(dev.writes)


# ---------------------------------------------------------------------------
# 3. Brightness validation - protocol layer (defence in depth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, 101, 255, 65535])
def test_build_backlight_packet_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):
        protocol.build_backlight_packet(value)


@pytest.mark.parametrize("value", [-1, 101, 255, 65535])
def test_build_init_packets_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError):
        protocol.build_init_packets(value)


@pytest.mark.parametrize("value", ["50", 50.0, None, True, b"50"])
def test_protocol_rejects_non_int_brightness(value: object) -> None:
    with pytest.raises(ValueError):
        protocol.build_backlight_packet(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        protocol.build_init_packets(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_protocol_accepts_in_range_brightness(value: int) -> None:
    packet = protocol.build_backlight_packet(value)
    assert len(packet) == protocol.PACKET_SIZE
    packets = protocol.build_init_packets(value)
    assert len(packets) == 2  # CRTDIS + CRTLIG
    assert all(len(p) == protocol.PACKET_SIZE for p in packets)


def test_backlight_value_is_encoded_little_endian_at_bytes_10_11() -> None:
    # The vendor capture shows CRTLIG bytes 10-11 as ``32 00`` (0x32 = 50), i.e.
    # little-endian, NOT ``00 32`` (handoff doc section 2, init-sequence table,
    # which the capture corrected). build_backlight_packet must match the wire.
    packet = protocol.build_backlight_packet(50)
    assert packet[5:10] == b"LIG\x00\x00"
    assert packet[10:12] == b"\x32\x00"  # 50, little-endian
    assert int.from_bytes(packet[10:12], "little") == 50
