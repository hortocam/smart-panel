"""Golden protocol tests against fixtures extracted from the USB capture (T005).

The fixtures in ``tests/fixtures/`` are the raw 1024-byte OUT packets the
vendor software actually sent (extracted by ``scripts/extract_fixtures.py``),
so these are ground-truth assertions rather than a re-derivation of the
encoder. Two kinds of test live here:

- **Fixture tests** pin the exact header bytes seen on the wire for CRTDIS,
  CRTLIG and CRTDRA.
- **Builder tests** pin ``panel_driver.protocol.build_frame_packets`` against
  the packet math the capture confirms: 32-byte preamble + JPEG, split into
  1024-byte packets with the last one zero-padded, and the uint16 length-field
  limit.

Header layout (from ``handoff/panel_protocol_handoff.md`` section 2):

    [0:5]   b"CRT\\x00\\x00"                    magic
    [5:10]  command, e.g. b"DIS\\x00\\x00"
    [10:12] per-command field (length for DRA, brightness for LIG)
    [12:14] b"\\xb1\\x00" for DRA, b"\\x00\\x00" for DIS/LIG
"""

from __future__ import annotations

from pathlib import Path

import pytest

from panel_driver.protocol import PACKET_SIZE, build_frame_packets

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CRTDIS = (FIXTURES / "crtdis_packet.bin").read_bytes()
CRTLIG = (FIXTURES / "crtlig_packet.bin").read_bytes()
CRTDRA = (FIXTURES / "crtdra_first_packet.bin").read_bytes()
FRAME_58697 = (FIXTURES / "frame_58697.jpg").read_bytes()

# (fixture, command name) pairs — bytes 5-9 on the wire.
COMMAND_FIXTURES = [
    (CRTDIS, b"DIS\x00\x00"),
    (CRTLIG, b"LIG\x00\x00"),
    (CRTDRA, b"DRA\x00\x00"),
]


# --- header bytes 0-9: magic + command --------------------------------------


@pytest.mark.parametrize("packet, command", COMMAND_FIXTURES)
def test_header_magic_is_crt_nul_nul(packet: bytes, command: bytes) -> None:
    assert packet[:5] == b"CRT\x00\x00"


@pytest.mark.parametrize("packet, command", COMMAND_FIXTURES)
def test_header_bytes_5_to_9_are_the_command(packet: bytes, command: bytes) -> None:
    assert packet[5:10] == command


def test_header_bytes_0_to_9_are_magic_plus_command() -> None:
    assert CRTDIS[:10] == b"CRT\x00\x00" + b"DIS\x00\x00"
    assert CRTLIG[:10] == b"CRT\x00\x00" + b"LIG\x00\x00"
    assert CRTDRA[:10] == b"CRT\x00\x00" + b"DRA\x00\x00"


# --- header bytes 12-13: flags ----------------------------------------------


def test_crtdra_flags_are_b1_00() -> None:
    assert CRTDRA[12:14] == b"\xb1\x00"


def test_crtdra_length_field_is_big_endian_14579() -> None:
    # The first captured frame carries a 14,547-byte JPEG: 32 + 14,547.
    assert int.from_bytes(CRTDRA[10:12], "big") == 14_579


@pytest.mark.parametrize("packet", [CRTDIS, CRTLIG])
def test_init_flags_are_00_00(packet: bytes) -> None:
    assert packet[12:14] == b"\x00\x00"


def test_crtdis_carries_no_payload_value() -> None:
    assert CRTDIS[10:12] == b"\x00\x00"


def test_crtlig_carries_brightness_50_in_bytes_10_11() -> None:
    # The capture shows the CRTLIG bytes as ``32 00`` (0x32 = 50). This is the
    # observed wire order; it differs from the ``\x00\x32`` written in handoff
    # doc section 2, which is a byte-order error in that table (reported).
    assert CRTLIG[10:12] == b"\x32\x00"
    assert int.from_bytes(CRTLIG[10:12], "little") == 50


# --- packet math (confirmed against the capture) ----------------------------


def test_synthetic_14547_byte_payload_gives_exactly_15_packets() -> None:
    # 14,547-byte near-solid-black JPEG -> 14,579 total -> 15 packets.
    packets = build_frame_packets(b"\x00" * 14_547)
    assert len(packets) == 15


def test_real_58697_byte_frame_gives_exactly_58_packets() -> None:
    assert len(FRAME_58697) == 58_697
    packets = build_frame_packets(FRAME_58697)
    assert len(packets) == 58


@pytest.mark.parametrize(
    "jpeg_len, expected_packets",
    [
        (14_547, 15),
        (58_697, 58),
        (0, 1),          # 32 bytes -> one zero-padded packet
        (992, 1),        # 1024 bytes exactly -> one packet
        (993, 2),        # one byte over -> two packets
    ],
)
def test_packet_count_is_ceil_of_preamble_plus_jpeg(
    jpeg_len: int, expected_packets: int
) -> None:
    packets = build_frame_packets(b"\x00" * jpeg_len)
    assert len(packets) == -(-(32 + jpeg_len) // PACKET_SIZE) == expected_packets


def test_every_packet_is_exactly_1024_bytes() -> None:
    packets = build_frame_packets(FRAME_58697)
    assert all(len(p) == 1024 for p in packets)


def test_first_packet_carries_the_crtdra_length_and_jpeg_soi() -> None:
    packets = build_frame_packets(FRAME_58697)
    first = packets[0]
    # big-endian uint16 at bytes 10-11 = 32 + len(jpeg)
    assert int.from_bytes(first[10:12], "big") == 32 + len(FRAME_58697)
    # the payload begins with the JPEG SOI marker the capture shows
    assert first[32:36] == b"\xff\xd8\xff\xe0" == FRAME_58697[:4]


# --- the uint16 length-field limit ------------------------------------------


def test_frame_too_large_raises_value_error() -> None:
    # 32 + 65,504 = 65,536 > 65,535.
    with pytest.raises(ValueError):
        build_frame_packets(b"\x00" * 65_504)


def test_largest_frame_that_fits_does_not_raise() -> None:
    # 32 + 65,503 = 65,535 fits exactly.
    packets = build_frame_packets(b"\x00" * 65_503)
    assert int.from_bytes(packets[0][10:12], "big") == 65_535
