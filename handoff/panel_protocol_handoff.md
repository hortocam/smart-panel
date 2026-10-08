# USB Bar Display — Protocol Reverse-Engineering Handoff

Purpose: drive a 9.16" IPS bar display (sold under white-label names via
mirabox.key123.vip / yeahmagicgaming.com) directly from a Python app on a
Raspberry Pi or Mac, bypassing the vendor Windows software entirely.

This doc captures everything confirmed from a USBPcap/Wireshark capture of
the vendor driver in normal operation, plus what's still open. Bring the two
reference files along with this doc into the new project:

- `test_pattern_1920x462.png` — synthetic diagnostic image used for the capture
- `extracted_frame_from_capture.jpg` — the actual JPEG payload pulled out of
  the wire capture for that test pattern, useful as a ground-truth reference
  for encoder settings and orientation

---

## 1. Device identification

| Property | Value |
|---|---|
| USB string descriptor | `HOTSPOTEKUSB HID DEMO` |
| VID / PID | `0x5548` / `0x1011` |
| USB interface class | HID (`0x03`) |
| USB speed | High-Speed (480 Mb/s) |
| Data endpoint | `0x01`, OUT, **Interrupt** transfer type |
| Max packet size | 1024 bytes (HS interrupt) |
| Physical connection | Internal USB 2.0 via motherboard 9-pin header (or 4-pin subset — power/D+/D-/GND). Not RS-232 serial despite appearances. |
| Native display buffer | **462 (W) × 1920 (H), portrait** — even though the physical panel is a wide bar. Vendor software presents a 1920×462 landscape canvas to the user and rotates internally before transmission. |

`fullpaneltest.pcapng` in this repo has been filtered to the panel's traffic
only (USB device address 9; 39,291 packets). The original capture also held
unrelated peripherals from the capture host (a fingerprint sensor, webcam, and
Bluetooth adapter), and the capture-host CPU/OS metadata; both were removed.

---

## 2. Wire protocol — confirmed

Each frame is pushed as a sequence of 1024-byte interrupt OUT packets to
endpoint `0x01`. The first packet of each frame starts with a fixed 33-byte
header immediately followed by a raw JPEG (JFIF) image; subsequent packets
are pure continuation bytes of the same JPEG stream with no per-packet
framing.

### Header layout (bytes 0–31, then JPEG at byte 32)

| Offset | Bytes | Meaning |
|---|---|---|
| 0–4 | `43 52 54 00 00` | ASCII `"CRT\0\0"` |
| 5–9 | `44 52 41 00 00` | ASCII `"DRA\0\0"` — together spells `CRTDRA`, almost certainly the command name |
| 10–11 | e.g. `38 f3` or `e5 69` | **big-endian uint16 = 32 + len(jpeg_bytes)** — verified exactly against two different frame sizes |
| 12–13 | `b1 00` | **Constant in both observed samples.** Purpose unknown — only 2 data points. Possible format/quality/channel flag. Needs more captures (different resolution/quality/theme) to confirm whether it ever varies. |
| 14–31 | all `0x00` | padding to fill the fixed 32-byte preamble |
| 32… | `FF D8 FF E0 …FF D9` | Standard baseline JFIF JPEG, 3 components, the actual image (portrait 462×1920 orientation) |

### Packet/frame math (confirmed exactly against two real samples)

```
total_content_len = 32 + len(jpeg_bytes)
num_packets       = ceil(total_content_len / 1024)
last packet is zero-padded to fill out to 1024 bytes
```

Verified: a 14,547-byte JPEG (near-solid-black test) → 14,579 total →
15 packets exactly. A 58,697-byte JPEG (busy test pattern) → 58,729 total →
58 packets exactly.

### Streaming behavior

- The vendor software **continuously re-sends frames** even for static
  content — this is a video-style push, not "set once and forget."
- *(measured)* 340 `CRTDRA` frames were sent in 19.3 s (**17.6 fps** average),
  with a **median frame interval of 61 ms**. A 58-packet frame (about 58 KB)
  transferred in a **median 13 ms**, roughly **0.23 ms per packet**. The vendor
  is therefore pacing itself rather than being bandwidth-limited by the link.
- **Measured on Windows** (the vendor-software capture host); hidapi on Linux
  (hidraw) and macOS may be slower, which is why the sustainable rate is
  re-measured on the target with `scripts/bench_throughput.py` (see decision 1
  in `research.md`).
- This supersedes the earlier per-packet-timing estimate in this section, which
  did not match the capture. Treat 17.6 fps as the observed vendor figure, not a
  hard requirement; the practical lower bound is still untested (see section 4).

### Init sequence (required before first frame)

A second capture (fullpaneltest.pcapng) revealed two mandatory init commands
that must be sent before the first CRTDRA frame. Without them, the panel's
USB controller goes unresponsive after ~60-70 seconds.

| Order | Command | Header bytes 5-9 | Bytes 10-11 | Flags (12-13) |
|---|---|---|---|---|
| 1 | CRTDIS | `DIS\x00\x00` | `\x00\x00` (zero) | `\x00\x00` |
| 2 | CRTLIG | `LIG\x00\x00` | uint16 brightness, **little-endian** (`\x32\x00` = 50) | `\x00\x00` |
| 3+ | CRTDRA | `DRA\x00\x00` | uint16 length = 32 + len(jpeg), **big-endian** | `\xb1\x00` |

The byte order of the bytes 10-11 field is **per command**: CRTDRA uses big-endian
(the capture's first frame reads `\x38\xf3` = 14,579 = 32 + 14,547), while CRTLIG uses
**little-endian** (brightness `\x32\x00` = 50; read as big-endian the same bytes are
12,800, i.e. out of range). CRTDIS carries an explicit zero in this field — it is not
the 32-byte preamble length.

> History: an earlier revision of this table wrote the CRTLIG example as `\x00\x32`
> (big-endian), which contradicted the capture it cites. The capture is authoritative
> (constitution Principle I); the table now matches it byte-for-byte, and
> `tests/unit/test_protocol.py::test_crtlig_carries_brightness_50_in_bytes_10_11`
> pins the wire order. The builder functions, however, did **not** follow in the
> same change: `build_init_packets()` still emitted big-endian brightness and wrote
> the 32-byte preamble length into the CRTDIS field. Both defects survived review
> because the golden tests only pinned the *fixtures*, never the builder. The T007
> change reconciles the builder with this table:
> `tests/unit/test_protocol_init_order.py` now asserts `build_init_packets(50)`
> reproduces the captured CRTDIS and CRTLIG packets **byte-for-byte**.

The CRTDIS and CRTLIG packets use the same 32-byte header format as CRTDRA
but with `\x00\x00` for bytes 12-13 instead of `\xb1\x00`. The payload
after the header is either empty (CRTDIS) or a 2-byte brightness value
(CRTLIG). The rest of the 1024-byte packet is zero-padded.

These commands are sent **once** at the start of a session, not periodically.
The vendor software sends them immediately after the HID GET_REPORT
(firmware version query) and before the first video frame.

### Value validation at the library boundary (T007 hardening)

Values that reach the wire are validated before any packet is built
(constitution Principle II — the panel's USB controller has been observed to
hang on a malformed stream, and the firmware is not user-recoverable). The
brightness boundary is enforced at **two levels on purpose**, so a regression in
one layer cannot put an out-of-range value on the wire:

| Value | Constraint | Enforced in | Failure mode |
|---|---|---|---|
| Backlight brightness | `int` in `0..100`; `bool` rejected | `protocol.validate_brightness`, called by `protocol.build_init_packets` / `protocol.build_backlight_packet` and re-checked by `device.init_display` / `device.set_backlight` | `ValueError`, and **no bytes are written** |
| Frame length | `32 + len(jpeg) <= 65535` (uint16 field at bytes 10–11) | `protocol.build_frame_packets` | `ValueError` |

`int` is required strictly: a `float`, numeric string, `None`, or `bool` is
rejected rather than coerced, so a mis-typed configuration cannot silently
truncate to a uint16 on the wire.

**CRTDIS and CRTLIG byte order (the reconciliation this change lands).** The
builders now reproduce the capture byte-for-byte, and the order is **per
command**:

- **CRTDIS** — bytes 10–11 are `00 00`, an explicit zero. The builder previously
  wrote the 32-byte preamble length there.
- **CRTLIG** — bytes 10–11 are the brightness **little-endian** (`32 00` = 50).
  The builder previously wrote it big-endian (`00 32`), contradicting the capture
  table above.
- **CRTDRA** — bytes 10–11 stay **big-endian** (`32 + len(jpeg)`); this was
  already correct.

The regression that let both defects through was that the golden tests pinned the
*fixtures* but never asserted the builder reproduced them;
`tests/unit/test_protocol_init_order.py` closes that gap.

**No byte sequence the vendor capture does not show may run on a default
path.** Only the three observed commands (`CRTDIS`, `CRTLIG`, `CRTDRA`) are
emitted, and every `dev.write` keeps the `b"\x00"` report-ID prefix (1025 bytes)
and stays inside `panel_driver/device.py` (asserted by a fake-device test that
records every write). Header bytes **12–13 remain unverified** (see the header
table above and open item 2): they are reproduced as the observed constant
(`b1 00` for CRTDRA, `00 00` for CRTDIS/CRTLIG), not treated as a decoded or
load-bearing field.

Brightness `0` is documented as "backlight off" but remains **unverified on
hardware** until the T129 smoke test records its observed effect; if `0` proves
unsafe or does not blank the panel, the lower bound becomes 1 and FR-011 is
amended.

### macOS hidapi report ID quirk (empirically discovered)

On macOS, hidapi's `write()` uses `IOHIDDeviceSetReport` which treats the
**first byte as the HID report ID** and sends the remaining bytes as data.
This means:

- `dev.write(1024_bytes)` → sends **1023 bytes** on the wire (byte 0 eaten as report ID)
- `dev.write(b"\\x00" + 1024_bytes)` → sends **1024 bytes** on the wire (0x00 = non-numbered report ID, then the actual data)

The vendor capture shows 1024-byte interrupt OUT packets with no report ID
prefix. To match this on macOS, **prepend `0x00` before each 1024-byte packet**
so the wire format is correct. The Python `hid` package on Linux does NOT
have this behaviour — it sends exactly what you give it.

**Workaround in code:**
```python
dev.write(b"\\x00" + packet)  # macOS: 0x00 RID + 1024 data = 1025 bytes total
```

---

## 3. Orientation — confirmed but direction not yet pinned down

- The vendor app's rotation setting was at **0%** for our test, yet the
  transmitted JPEG was portrait (462×1920), not landscape (1920×462). This
  confirms the app **always** internally rotates a landscape canvas to the
  panel's native portrait buffer before encoding, regardless of the
  "rotation" UI setting (that setting is presumably for further
  user-requested rotation on top of this baseline transform).
- When an already-portrait image was fed directly into the app, it was
  center-cropped/fit instead of rotated again — confirming the app expects
  landscape input and does the rotation itself exactly once.
- **Open item:** which direction (CW vs. CCW) it rotates is not yet
  confirmed from static analysis. This needs one empirical round-trip test:
  send a frame with clear corner labels (the test pattern PNG works) with a
  guessed rotation, observe the physical panel, flip the flag if wrong.

---

## 4. Open questions to resolve early in the new project

1. ~~**Rotation direction** (see above) — quick empirical test, do this first.~~
   **RESOLVED:** `clockwise=True` (270° CCW / `Image.ROTATE_270`) is correct.
2. **Meaning of header bytes 12–13** (`b1 00`) — capture one more theme at a
   different resolution/quality to see if this changes. If it never
   changes across resolutions, it's very likely safe to hardcode.
3. ~~**Device init sequence** — the original capture's first ~24 packets~~
   ~~(before the first `CRTDRA` frame) were HID descriptor/config exchanges~~
   ~~during enumeration, not inspected in detail yet. Worth checking whether~~
   ~~there's a required SET_REPORT/feature-report handshake before the~~
   ~~device will accept streamed frames, or whether you can just start~~
   ~~writing `CRTDRA` frames cold after opening the HID handle.~~
   **RESOLVED:** Two init commands must be sent before the first CRTDRA frame:
   - `CRTDIS` — display init (32-byte header + zeros, flags `\x00\x00`)
   - `CRTLIG` — backlight on (32-byte header + uint16 brightness value, little-endian, flags `\x00\x00`)
   Without these, the panel's USB controller goes unresponsive after ~60-70s.
4. **Minimum sustained frame rate** — untested whether the panel
   blanks/times out if frames stop coming, and what the practical minimum
   push rate is.
5. ~~**Any IN-endpoint traffic** — not yet checked whether the device sends~~
   ~~anything back (status/ack/button-press data if this panel has any~~
   ~~physical controls) that the app reads.~~
   **RESOLVED:** No IN-endpoint data observed during streaming. The device is write-only.

---

## 5. Proposed Python application architecture

Goal: a small library + a stats-rendering layer on top, portable between
Raspberry Pi (Linux) and Mac.

```
panel_driver/
├── README.md
├── pyproject.toml
├── panel_driver/
│   ├── __init__.py
│   ├── device.py        # HID open/close, VID/PID constants, endpoint write loop
│   ├── protocol.py       # build_crtdra_frame(jpeg_bytes) -> list[bytes] packets
│   ├── render.py         # PIL-based composition: background + overlays -> Image
│   ├── rotation.py       # landscape canvas -> native portrait transform
│   └── stream.py         # refresh loop: render -> encode -> protocol -> device.write
├── scripts/
│   ├── send_test_pattern.py   # one-shot: send the known test PNG, verify round trip
│   └── probe_init_sequence.py # replay/inspect the pre-stream HID handshake, if any
└── assets/
    ├── test_pattern_1920x462.png
    └── extracted_frame_from_capture.jpg
```

### Core dependencies
- `hidapi` (Python `hid` package) — opens the device by VID/PID and writes
  to the interrupt OUT endpoint without needing a kernel driver. Works on
  both Raspberry Pi OS and macOS.
  - **Linux/Pi note:** add a udev rule for `0x5548:0x1011` so it's
    accessible without root (`/etc/udev/rules.d/99-panel.rules`, `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="5548", ATTRS{idProduct}=="1011", MODE="0666"`).
- `Pillow` — compose backgrounds + overlays (clock, CPU/mem/temp text or
  graphs) and JPEG-encode at the end.
- `psutil` (Pi-side) or a small SSH/HTTP fetch (if pulling NAS stats
  remotely) for the actual stats content.

### `protocol.py` — core framing logic (skeleton)

```python
HEADER_MAGIC = b"CRT\x00\x00" + b"DRA\x00\x00"  # bytes 0-9
PREAMBLE_LEN = 32
PACKET_SIZE = 1024

def build_frame_packets(jpeg_bytes: bytes) -> list[bytes]:
    total_len = PREAMBLE_LEN + len(jpeg_bytes)
    length_field = total_len.to_bytes(2, "big")
    header = HEADER_MAGIC + length_field + b"\xb1\x00"  # bytes 12-13: TBD, hardcoded for now
    header = header.ljust(PREAMBLE_LEN, b"\x00")
    payload = header + jpeg_bytes
    # pad to a whole number of 1024-byte packets
    pad_len = (-len(payload)) % PACKET_SIZE
    payload += b"\x00" * pad_len
    return [payload[i:i+PACKET_SIZE] for i in range(0, len(payload), PACKET_SIZE)]
```

### `rotation.py` — orientation transform

```python
from PIL import Image

def to_panel_native(landscape_img: Image.Image, clockwise: bool = True) -> Image.Image:
    # landscape_img expected at 1920x462
    return landscape_img.transpose(
        Image.ROTATE_270 if clockwise else Image.ROTATE_90
    )
```
(`clockwise` flag is the empirical unknown from section 3 — flip and test.)

### `stream.py` — refresh loop sketch

```python
import hid, io, time
from .protocol import build_frame_packets
from .rotation import to_panel_native

VID, PID = 0x5548, 0x1011

def run(render_fn, fps=15, clockwise=True):
    dev = hid.device()
    dev.open(VID, PID)
    interval = 1.0 / fps
    try:
        while True:
            t0 = time.time()
            frame_img = render_fn()                    # returns 1920x462 PIL Image
            rotated = to_panel_native(frame_img, clockwise)
            buf = io.BytesIO()
            rotated.save(buf, format="JPEG", quality=85)
            packets = build_frame_packets(buf.getvalue())
            for pkt in packets:
                dev.write(pkt)
            time.sleep(max(0, interval - (time.time() - t0)))
    finally:
        dev.close()
```

---

## 6. Recommended next steps, in order

1. **Bring this doc + the two reference images into the new Claude Code
   project.**
2. Write `scripts/send_test_pattern.py`: open the device via `hidapi`,
   build one frame from the known test pattern PNG (rotate one direction,
   encode, frame, send once), and observe the physical panel. Flip the
   rotation flag if it's backwards. This single test resolves the biggest
   open unknown.
3. Once orientation is confirmed, get a bare `stream.py` loop pushing a
   static image continuously and confirm the panel doesn't blank or
   glitch over a sustained period (checks the "does it need continuous
   refresh" and "what's a safe FPS" questions empirically).
4. Only after the transport is proven solid, build out `render.py` with
   actual stats content (CPU/mem/temp, clock, background image/video —
   note: video backgrounds imply this same protocol just gets called at
   higher FPS with a new JPEG each time, which your loop already supports).
5. If you want to eventually confirm header bytes 12–13 aren't load-bearing
   in some edge case, capture one more vendor-software session at a
   different quality/resolution setting and diff against this doc's table.
