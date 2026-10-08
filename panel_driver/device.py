"""HID device open/close and write operations for the USB bar display.

VID/PID: 0x5548 / 0x1011
String descriptor: "HOTSPOTEKUSB HID DEMO"
Endpoint: 0x01, OUT, Interrupt transfer, 1024-byte packets.

macOS note: hidapi's write() treats the first byte as the HID report ID.
For non-numbered reports, prepend 0x00 before the 1024-byte packet data.
Each write is therefore 1025 bytes: [0x00] + [1024 bytes of frame data].

Init sequence (required before first frame):
  1. CRTDIS — display init
  2. CRTLIG — backlight on
  3. CRTDRA — actual frame (repeated for streaming)

``import hid`` is deliberately deferred into :func:`open_device` so importing
this module (and the pure protocol code it re-exports) needs no hidapi library
installed. Only a real :func:`open_device` call requires hidapi.
"""

# Defer annotation evaluation so ``hid.Device`` never has to be imported at module
# level just to write the annotations below.
from __future__ import annotations

from typing import Any

from .protocol import (
    build_backlight_packet,
    build_init_packets,
    validate_brightness,
)

VID = 0x5548
PID = 0x1011


def open_device() -> Any:
    """Open the USB bar display HID device.

    ``import hid`` lives here, not at module scope, so importing
    :mod:`panel_driver.device` does not require the hidapi library (constitution
    Principle IV: the protocol code must be importable and testable with no
    hardware support installed).

    Returns:
        An open ``hid.Device`` ready for interrupt OUT writes.

    Raises:
        IOError: If the device is not found or cannot be opened.
    """
    import hid  # deferred so importing the module needs no hidapi library

    return hid.Device(VID, PID)


def init_display(dev: Any, brightness: int = 50) -> None:
    """Send the init sequence (CRTDIS + CRTLIG) to the display.

    Must be called before the first CRTDRA frame.

    Args:
        dev: An open ``hid.Device``.
        brightness: Backlight brightness (0–100, default 50).

    Raises:
        ValueError: If ``brightness`` is not an int in 0..100. The boundary is
            validated here as well as in ``protocol`` on purpose
            (constitution Principle II: values written to the device are
            validated at the library boundary).
    """
    validate_brightness(brightness)
    for pkt in build_init_packets(brightness):
        dev.write(b"\x00" + pkt)


def set_backlight(dev: Any, brightness: int) -> None:
    """Set the backlight brightness at any time.

    Args:
        dev: An open ``hid.Device``.
        brightness: 0 = off, 100 = max.

    Raises:
        ValueError: If ``brightness`` is not an int in 0..100.
    """
    validate_brightness(brightness)
    pkt = build_backlight_packet(brightness)
    dev.write(b"\x00" + pkt)


def write_frame(dev: Any, packets: list[bytes]) -> None:
    """Write a complete frame (sequence of 1024-byte packets) to the device.

    On macOS, hidapi prepends a HID report ID byte. We send 0x00 (non-numbered
    report) + 1024 bytes of packet data = 1025 bytes per write.

    Args:
        dev: An open ``hid.Device``.
        packets: List of 1024-byte packets from
            ``protocol.build_frame_packets()``.
    """
    for pkt in packets:
        # macOS hidapi: first byte = report ID (0x00 for non-numbered)
        dev.write(b"\x00" + pkt)
