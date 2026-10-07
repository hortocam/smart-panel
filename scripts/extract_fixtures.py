#!/usr/bin/env python3
"""Extract golden wire-protocol fixtures from the vendor USB capture (T004).

Reads ``handoff/fullpaneltest.pcapng`` with ``tshark`` and writes the raw
1024-byte OUT packets that the panel actually received, so the protocol tests
can assert against ground truth instead of a re-derivation of it:

  tests/fixtures/crtdis_packet.bin        first CRTDIS init packet
  tests/fixtures/crtlig_packet.bin        first CRTLIG init packet (brightness 50)
  tests/fixtures/crtdra_first_packet.bin  first packet of the first CRTDRA frame
  tests/fixtures/frame_58697.jpg          copy of handoff/extracted_frame_from_capture.jpg

The capture filter is the one named in the task:

    usb.endpoint_address==0x01 && usb.data_len>0

which selects the panel's 1024-byte interrupt-OUT transfers. On this host it
resolves 19,625 frames; the first three are CRTDIS, CRTLIG (brightness value
``0x32`` = 50) and CRTDRA (length field ``0x38f3`` = 14,579 = 32 + 14,547,
flags ``b1 00``, JPEG SOI ``ff d8 ff e0``). The script reports a disagreement
loudly rather than writing a fixture that does not match the capture.

Requires ``tshark`` on PATH. Re-runnable and idempotent.

Usage::

    python scripts/extract_fixtures.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PCAP = REPO_ROOT / "handoff" / "fullpaneltest.pcapng"
EXTRACTED_JPEG = REPO_ROOT / "handoff" / "extracted_frame_from_capture.jpg"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

# The task's filter, verbatim.
CAPTURE_FILTER = "usb.endpoint_address==0x01 && usb.data_len>0"

COMMAND_OFFSET = 5  # bytes 5-9 hold the command name
PACKET_SIZE = 1024

# What the capture is expected to show for the init packets (from the T004
# brief and the handoff doc section 2). Used only to sanity-check the source,
# never to overwrite what the capture actually says.
EXPECTED = {
    b"DIS\x00\x00": {"brightness_or_len": bytes.fromhex("0000")},
    b"LIG\x00\x00": {"brightness_or_len": bytes.fromhex("3200")},  # 0x32 = 50
}


def _run_tshark() -> list[bytes]:
    """Return the usbhid.data payloads of every matching OUT frame, in order."""
    try:
        proc = subprocess.run(
            [
                "tshark",
                "-r",
                str(PCAP),
                "-Y",
                CAPTURE_FILTER,
                "-T",
                "fields",
                "-e",
                "usbhid.data",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        sys.exit("error: tshark is not on PATH; install Wireshark/tshark first")
    except subprocess.CalledProcessError as exc:  # pragma: no cover - host issue
        sys.exit(f"error: tshark failed ({exc.returncode}): {exc.stderr.strip()}")

    packets = [bytes.fromhex(line) for line in proc.stdout.splitlines() if line.strip()]
    if not packets:
        sys.exit(f"error: filter matched no frames in {PCAP.name}")
    return packets


def _first_packet(packets: list[bytes], command: bytes) -> bytes:
    """Return the first packet whose command name equals ``command``."""
    for pkt in packets:
        if pkt[COMMAND_OFFSET : COMMAND_OFFSET + 5] == command:
            return pkt
    sys.exit(f"error: no {command!r} packet found in the capture")


def main() -> int:
    if not PCAP.exists():
        sys.exit(f"error: capture not found: {PCAP}")
    if not EXTRACTED_JPEG.exists():
        sys.exit(f"error: extracted JPEG not found: {EXTRACTED_JPEG}")

    FIXTURES.mkdir(parents=True, exist_ok=True)

    packets = _run_tshark()
    print(f"{PCAP.name}: {len(packets)} frames match the filter")

    dis = _first_packet(packets, b"DIS\x00\x00")
    lig = _first_packet(packets, b"LIG\x00\x00")
    dra = _first_packet(packets, b"DRA\x00\x00")

    for name, pkt, command in (
        ("crtdis_packet.bin", dis, b"DIS\x00\x00"),
        ("crtlig_packet.bin", lig, b"LIG\x00\x00"),
        ("crtdra_first_packet.bin", dra, b"DRA\x00\x00"),
    ):
        if len(pkt) != PACKET_SIZE:
            sys.exit(f"error: {name}: expected {PACKET_SIZE} bytes, got {len(pkt)}")
        if pkt[COMMAND_OFFSET : COMMAND_OFFSET + 5] != command:
            sys.exit(f"error: {name}: unexpected command {pkt[5:10]!r}")
        dst = FIXTURES / name
        dst.write_bytes(pkt)
        print(
            f"wrote {dst.relative_to(REPO_ROOT)} ({len(pkt)} bytes) "
            f"cmd={command!r} bytes10_11={pkt[10:12].hex()} flags={pkt[12:14].hex()}"
        )

    # Sanity-check the fields T004 and T005 rely on, and report disagreements
    # instead of silently writing a fixture that contradicts the capture.
    for command, expected in EXPECTED.items():
        pkt = {b"DIS\x00\x00": dis, b"LIG\x00\x00": lig}[command]
        if pkt[10:12] != expected["brightness_or_len"]:
            print(
                f"NOTE: {command!r} bytes 10-11 are {pkt[10:12].hex()}, "
                f"expected {expected['brightness_or_len'].hex()} from the brief",
                file=sys.stderr,
            )

    dst_jpg = FIXTURES / "frame_58697.jpg"
    shutil.copyfile(EXTRACTED_JPEG, dst_jpg)
    print(f"copied {dst_jpg.relative_to(REPO_ROOT)} ({dst_jpg.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
