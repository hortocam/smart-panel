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
"""

import hid

from .protocol import build_init_packets

VID = 0x5548
PID = 0x1011


def open_device() -> hid.Device:
    """Open the USB bar display HID device.

    Returns:
        An open hid.Device ready for interrupt OUT writes.

    Raises:
        IOError: If the device is not found or cannot be opened.
    """
    return hid.Device(VID, PID)


def init_display(dev: hid.Device, brightness: int = 50) -> None:
    """Send the init sequence (CRTDIS + CRTLIG) to the display.

    Must be called before the first CRTDRA frame.

    Args:
        dev: An open hid.Device.
        brightness: Backlight brightness (0–100, default 50).
    """
    init_packets = build_init_packets(brightness)
    for pkt in init_packets:
        dev.write(b"\x00" + pkt)


def set_backlight(dev: hid.Device, brightness: int) -> None:
    """Set the backlight brightness at any time.

    Args:
        dev: An open hid.Device.
        brightness: 0 = off, 100 = max.
    """
    from .protocol import build_backlight_packet
    pkt = build_backlight_packet(brightness)
    dev.write(b"\x00" + pkt)


def write_frame(dev: hid.Device, packets: list[bytes]) -> None:
    """Write a complete frame (sequence of 1024-byte packets) to the device.

    On macOS, hidapi prepends a HID report ID byte. We send 0x00 (non-numbered
    report) + 1024 bytes of packet data = 1025 bytes per write.

    Args:
        dev: An open hid.Device.
        packets: List of 1024-byte packets from protocol.build_frame_packets().
    """
    for pkt in packets:
        # macOS hidapi: first byte = report ID (0x00 for non-numbered)
        dev.write(b"\x00" + pkt)
