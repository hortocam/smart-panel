"""Fixture-vs-builder regression tests for the init commands (T007).

The golden tests above pin the *fixtures* (what the vendor actually put on the
wire) but never asserted that ``build_init_packets`` reproduced them. That gap
let a byte-order defect survive review: the builder encoded the CRTLIG brightness
big-endian (``b"\\x00\\x32"``) while the capture shows ``b"\\x32\\x00"``
(little-endian), and it wrote the 32-byte preamble length into CRTDIS bytes 10-11
where the capture has an explicit zero.

This module closes the gap: it asserts the builder output is byte-for-byte equal
to the captured CRTDIS and CRTLIG packets. The fixtures are ground truth
(constitution Principle I); the builder must reproduce them, not the reverse.
"""

from __future__ import annotations

from pathlib import Path

from panel_driver.protocol import build_init_packets

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CRTDIS = (FIXTURES / "crtdis_packet.bin").read_bytes()
CRTLIG = (FIXTURES / "crtlig_packet.bin").read_bytes()


def test_build_init_packets_reproduces_crtdis_byte_for_byte() -> None:
    dis, _ = build_init_packets(50)
    assert dis == CRTDIS


def test_build_init_packets_reproduces_crtlig_byte_for_byte() -> None:
    _, lig = build_init_packets(50)
    assert lig == CRTLIG


def test_build_init_packets_reproduces_the_whole_sequence_byte_for_byte() -> None:
    assert build_init_packets(50) == [CRTDIS, CRTLIG]


def test_crtdis_bytes_10_11_are_zero_not_the_preamble_length() -> None:
    # The capture carries an explicit zero in the DIS field; it is not 32.
    dis, _ = build_init_packets(50)
    assert dis[10:12] == b"\x00\x00"
    assert int.from_bytes(dis[10:12], "little") != 32


def test_crtlig_brightness_is_little_endian_on_the_wire() -> None:
    _, lig = build_init_packets(50)
    assert lig[10:12] == b"\x32\x00"
    assert int.from_bytes(lig[10:12], "little") == 50
