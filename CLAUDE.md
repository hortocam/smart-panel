# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python driver for a HOTSPOTEK USB HID bar display (VID/PID `0x5548:0x1011`), reverse-engineered from a USBPcap capture of the vendor Windows software. Target hosts are macOS and Raspberry Pi/Linux. The eventual goal is a stats-rendering layer (clock, CPU/mem/temp) on top of the transport; only the transport exists so far. See `README.md` for install, hardware, and the Linux udev rule.

`handoff/panel_protocol_handoff.md` is the authoritative protocol write-up (header layout, init sequence, open questions). Read it before changing anything in `panel_driver/`. Note that its section 5 "proposed architecture" is partly aspirational: `render.py` and `probe_init_sequence.py` do not exist.

## Commands

Setup from a fresh checkout: create and activate a virtualenv, then `pip install -e ".[dev]"` installs the package editable with everything the tests need (`pytest`, `ruff`, `psutil`, `defusedxml`). There is a test suite (pytest), a linter (ruff), and CI (`.github/workflows/ci.yml`): `ruff check .` must pass and `pytest` must pass with no panel attached (hardware-marked tests are skipped by default). The transport-only install without test tooling is `pip install -e .`.

The project is MIT licensed (`LICENSE`), with a `CODE_OF_CONDUCT.md`. The project constitution lives at `.specify/memory/constitution.md` and governs spec-driven work (Spec Kit skills: `/speckit-specify`, `/speckit-plan`, etc.).

```bash
source .venv/bin/activate
pip install -e ".[dev]"        # editable install + test tooling; ".[app]" adds psutil/defusedxml for the application layer

ruff check .                   # lint (configured in pyproject.toml [tool.ruff]); must be clean
pytest                         # non-hardware suite (the hardware marker is deselected by default)
pytest -m "not hardware"       # the same selection spelled out, as CI runs it

python scripts/send_test_pattern.py [--ccw] [--loop N]   # one-shot/short orientation check (needs the panel)
python scripts/stream.py           # stream assets/landscape_bg.jpg, retries open for 60s (needs the panel)
python scripts/stream_test.py      # stream test pattern after pressing Enter (needs the panel)
python scripts/stream_retry.py     # stream test pattern, retries open until device appears (needs the panel)
python scripts/extract_fixtures.py # rebuild tests/fixtures/*.bin/*.jpg from handoff/fullpaneltest.pcapng (needs tshark)
python scripts/bench_throughput.py # packets/s, frame times, sustainable fps (hardware-only; see its docstring)
```

Run scripts from the repo root: `stream_test.py` and `stream_retry.py` open `assets/test_pattern_1920x462.png` by relative path. Each script prepends the repo root to `sys.path`, so the package does not need to be installed. The committed `tests/fixtures/` mean `pytest` runs with neither a panel nor `tshark`.

CI (`.github/workflows/ci.yml`) runs `ruff check .` and `pytest -m "not hardware"` on Linux and macOS with Python 3.11 and 3.13, on every push to `main` and every pull request. The `smart-panel` console entry point is declared in `pyproject.toml`, but the CLI itself lands with the Phase 3 `smart_panel` application layer; until then the scripts above are the way to exercise the panel.

## Active feature

Spec-driven work lives in `specs/001-status-display-platform/`. The plan adds a `smart_panel` application layer (runner, plugins, CLI) on top of `panel_driver`; it is not implemented yet, so the Architecture section below describes only the existing transport. The plan pointer at the bottom of this file is managed by the agent-context extension (refresh with `uv run --no-project --with pyyaml bash .specify/extensions/agent-context/scripts/bash/update-agent-context.sh`, since the script needs PyYAML).

## Orchestration

Agents (orchestrator, Engineer, Reviewer, hardware agent) should read `docs/orchestration.md` for roles, card breakdown, per-card flow, repository rules, and the hardware handoff, and `docs/hermes-setup.md` for environment prerequisites. `main` will be protected once the owner applies T133 (pull request and CI required, no force pushes); until then, still use pull requests for every change and never push to `main`.

## Architecture

Pipeline: **landscape 1920×462 PIL image → `rotation.to_panel_native` → JPEG encode → `protocol.build_frame_packets` → `device.write_frame`**. `stream.run()` wires this into a render-callback loop. The scripts in `scripts/` duplicate this pipeline by hand instead of calling `stream.run()`.

- `protocol.py`: builds 1024-byte packets. Every command (`CRTDIS`, `CRTLIG`, `CRTDRA`) shares a 32-byte header: `CRT\0\0` + 5-byte command name + a per-command uint16 at bytes 10–11 (big-endian for CRTDRA, little-endian for CRTLIG) + 2 flag bytes at 12–13 + zero padding. The uint16 means different things per command: `32 + len(jpeg)` for DRA, the brightness (0–100) for LIG, and an explicit zero for DIS. DRA flags are `b1 00` (an unexplained constant); DIS/LIG flags are `00 00`. A JPEG frame can be at most 65535−32 bytes because of the uint16, so lower the JPEG quality if `build_frame_packets` raises `ValueError`.
- `device.py`: wraps the `hid` package. `init_display()` (CRTDIS then CRTLIG) must be sent once per session before the first frame, or the panel's USB controller hangs after about 60–70s. The panel is write-only and has no IN traffic.
- `rotation.py`: the panel's native buffer is portrait 462×1920 even though it is physically a wide bar, so landscape canvases are rotated before encoding. `clockwise=True` (`Image.ROTATE_270`) is the empirically confirmed correct direction. Always pass it, which is the default.
- `stream.py`: the loop that re-sends frames at a fixed FPS (default 15, JPEG quality 70). The vendor software re-sends continuously even for static content, so never send a frame once and stop.

### Gotchas

- **hidapi report-ID quirk:** every write must be `b"\x00" + packet` (1025 bytes). hidapi on macOS treats byte 0 as the report ID and strips it, so without the prefix only 1023 bytes reach the wire. `device.write_frame`/`init_display`/`set_backlight` already do this. Any new code that calls `dev.write` directly must do the same (`stream_retry.py` does).
- Calling `build_frame_packets` produces a pre-padded list, and the last packet is zero-padded to 1024 bytes. Don't pad again.
- The panel appears to need a power-cycle to recover from a hung state, which is why the scripts include "power-cycle the panel now" retry-open loops.
- Still-open questions (see handoff doc section 4): the meaning of header bytes 12–13, and the minimum sustained frame rate.

<!-- SPECKIT START -->
For additional context about technologies to be used, project structure,
shell commands, and other important information, read the current plan
at specs/001-status-display-platform/plan.md
<!-- SPECKIT END -->
