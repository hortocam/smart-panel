"""Build CRTDRA frame packets from JPEG bytes.

Wire protocol (reverse-engineered from USBPcap capture):
  - Each frame is a sequence of 1024-byte interrupt OUT packets.
  - First packet: 32-byte header + start of JPEG.
  - Subsequent packets: pure continuation of the JPEG stream.
  - Last packet zero-padded to 1024 bytes.

Init sequence (must be sent before first frame):
  1. CRTDIS  — display init (field at bytes 10-11 is zero, no payload)
  2. CRTLIG  — backlight on (brightness in bytes 10-11, little-endian)
  3. CRTDRA  — the actual JPEG frame (repeated for streaming)

Header layout (bytes 0–31):
  [0:5]   b"CRT\\x00\\x00"
  [5:10]  command name, e.g. b"DRA\\x00\\x00", b"DIS\\x00\\x00", b"LIG\\x00\\x00"
  [10:12] per-command uint16 field (see below for byte order)
  [12:14] b"\\xb1\\x00" for DRA, b"\\x00\\x00" for DIS/LIG
  [14:32] zero padding
  [32:]   payload (JPEG for DRA, empty for DIS/LIG)

The byte order of the bytes 10-11 field is **per command** and is taken from the
vendored USB capture (handoff doc section 2), not assumed:
  - CRTDRA: big-endian uint16 = 32 + len(jpeg_bytes)
  - CRTLIG: little-endian uint16 brightness (0-100); capture shows ``32 00`` = 50
  - CRTDIS: an explicit zero (``00 00``), NOT the 32-byte preamble length
"""

import struct

HEADER_MAGIC = b"CRT\x00\x00"  # bytes 0–4
PREAMBLE_LEN = 32
PACKET_SIZE = 1024
# Bytes 12–13: observed constant for CRTDRA frames across two different frame sizes.
# For CRTDIS and CRTLIG these are 0x00 0x00.
# UNVERIFIED: the meaning of the b1 00 pair has only two sample points in the
# vendor capture; it is treated as a constant, not a decoded field (handoff
# doc section 4, item 2).
HEADER_FLAGS_DRA = b"\xb1\x00"
HEADER_FLAGS_INIT = b"\x00\x00"

# Brightness (CRTLIG bytes 10–11) is written as a uint16 but only 0..100 is
# meaningful; the boundary is enforced here and again in device.py.
BRIGHTNESS_MIN = 0
BRIGHTNESS_MAX = 100


def validate_brightness(brightness: int) -> int:
    """Validate a backlight brightness value.

    Args:
        brightness: Candidate brightness.

    Returns:
        The same value when it is a valid int in 0..100.

    Raises:
        ValueError: If ``brightness`` is not an ``int`` (``bool`` included, as
            it is a subclass of ``int`` but not a brightness) or is outside
            0..100. The panel's USB controller has been observed to hang on a
            malformed stream, so the value is rejected at the library boundary
            before any packet is built (constitution Principle II).
    """
    if isinstance(brightness, bool) or not isinstance(brightness, int):
        raise ValueError(
            f"brightness must be an int in {BRIGHTNESS_MIN}..{BRIGHTNESS_MAX}, "
            f"got {type(brightness).__name__} ({brightness!r})"
        )
    if not BRIGHTNESS_MIN <= brightness <= BRIGHTNESS_MAX:
        raise ValueError(
            f"brightness must be in {BRIGHTNESS_MIN}..{BRIGHTNESS_MAX}, "
            f"got {brightness}"
        )
    return brightness


def _build_command_packet(
    command: bytes,
    value: int = 0,
    payload: bytes = b"",
    byte_order: str = "big",
) -> bytes:
    """Build a single 1024-byte packet for a CRT command.

    Args:
        command: 5-byte command name (e.g. b"DRA\\x00\\x00", b"DIS\\x00\\x00", b"LIG\\x00\\x00")
        value: For DRA: 32 + len(payload). For LIG: brightness (0-100). For DIS: 0.
        payload: JPEG bytes for DRA, empty for DIS/LIG.
        byte_order: ``"big"`` (CRTDRA) or ``"little"`` (CRTLIG) for the uint16 at
            bytes 10-11. The capture shows the order differs per command, so it is
            an explicit argument rather than a single module-wide default.

    Returns:
        A single 1024-byte packet.
    """
    fmt = ">H" if byte_order == "big" else "<H"
    header = HEADER_MAGIC + command + struct.pack(fmt, value)
    # Use DRA flags for DRA commands, init flags for DIS/LIG
    if command == b"DRA\x00\x00":
        header += HEADER_FLAGS_DRA
    else:
        header += HEADER_FLAGS_INIT
    header = header.ljust(PREAMBLE_LEN, b"\x00")
    packet = header + payload
    pad_len = (-len(packet)) % PACKET_SIZE
    packet += b"\x00" * pad_len
    return packet


def build_init_packets(brightness: int = 50) -> list[bytes]:
    """Build the init sequence packets (CRTDIS + CRTLIG).

    Must be sent before the first CRTDRA frame.

    Args:
        brightness: Backlight brightness value (0–100, default 50).
                    Use 0 to turn off the backlight.

    Returns:
        List of 2 packets: [CRTDIS, CRTLIG]

    Raises:
        ValueError: If ``brightness`` is not an int in 0..100.
    """
    validate_brightness(brightness)
    # CRTDIS carries an explicit zero in bytes 10-11: the capture shows ``00 00``,
    # not the 32-byte preamble length.
    dis = _build_command_packet(b"DIS\x00\x00", value=0, byte_order="little")
    # CRTLIG uses bytes 10-11 for brightness, little-endian on the wire
    # (capture: ``32 00`` = 50).
    lig = _build_command_packet(b"LIG\x00\x00", value=brightness, byte_order="little")
    return [dis, lig]


def build_backlight_packet(brightness: int) -> bytes:
    """Build a single CRTLIG packet to set backlight brightness.

    Can be sent at any time to adjust brightness or turn off the backlight.

    Args:
        brightness: Backlight brightness (0 = off, 100 = max).

    Returns:
        A single 1024-byte packet.

    Raises:
        ValueError: If ``brightness`` is not an int in 0..100.
    """
    validate_brightness(brightness)
    # Little-endian, matching the captured CRTLIG packet.
    return _build_command_packet(b"LIG\x00\x00", value=brightness, byte_order="little")


def build_frame_packets(jpeg_bytes: bytes) -> list[bytes]:
    """Split a JPEG into CRTDRA frame packets ready for HID interrupt OUT.

    Args:
        jpeg_bytes: Raw baseline JFIF JPEG bytes (portrait 462×1920).

    Returns:
        List of 1024-byte packets to write sequentially to endpoint 0x01.

    Raises:
        ValueError: If the total frame (32-byte header + JPEG) exceeds 65535
        bytes, which would overflow the uint16 length field in the header.
    """
    total_len = PREAMBLE_LEN + len(jpeg_bytes)
    if total_len > 65535:
        raise ValueError(
            f"Frame too large: {total_len} bytes exceeds uint16 max (65535). "
            f"JPEG is {len(jpeg_bytes)} bytes — reduce quality or resolution."
        )

    # Build the first packet with CRTDRA header (big-endian length field).
    first = _build_command_packet(b"DRA\x00\x00", value=total_len, payload=jpeg_bytes)

    # Split into 1024-byte packets
    return [first[i:i+PACKET_SIZE] for i in range(0, len(first), PACKET_SIZE)]
