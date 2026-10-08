# smart-panel

Drive a HOTSPOTEK USB HID bar display (a 9.16" IPS panel sold white-label via
mirabox.key123.vip / yeahmagicgaming.com) directly from Python, and turn it into
a managed, always-on status board: a news-style **crawl**, **gauges** for system
health, real-time **alerts** pushed by an agent or any local process, and a
**sidebar** of app icons with count chips.

The project ships two Python packages in one distribution:

- **`panel_driver`** — a minimal transport library: reverse-engineered USB HID
  framing, the init sequence, orientation, and the frame loop. It depends only
  on `hid` and `Pillow`.
- **`smart_panel`** — the application layer built on top of the transport: a
  long-running **runner** that composes a 1920×462 canvas from plugin regions
  and pushes it through a pluggable sink (real panel, PNG files, or null), a
  **plugin SDK** and loader, a **CLI** that is a thin client of a local
  Unix-socket control channel, and a durable SQLite state store.

The four requested features (alerts, crawl, gauges, sidebar) ship as built-in
plugins that use the same extension contract available to third parties, so the
core contains no feature-specific logic.

## Supported hardware

| Property | Value |
|---|---|
| USB VID / PID | `0x5548` / `0x1011` |
| USB string descriptor | `HOTSPOTEKUSB HID DEMO` |
| Interface / endpoint | HID (`0x03`), endpoint `0x01`, OUT, interrupt |
| Native display buffer | 462 × 1920 (portrait); the app presents a 1920 × 462 landscape canvas and rotates internally |
| Supported hosts | Raspberry Pi OS / Linux, and macOS |

Protocol facts (header layout, init sequence, open questions) are captured in
[`handoff/panel_protocol_handoff.md`](handoff/panel_protocol_handoff.md). That
document is the source of truth for anything on the wire; read it before
changing `panel_driver/`.

## Install

Python 3.11 or newer is required.

```bash
git clone https://github.com/hortocam/smart-panel.git
cd smart-panel
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

`pip install -e .` installs the transport with only the `hid` and `Pillow`
runtime dependencies. Two extras are available:

- **`app`** — adds the application-layer dependencies `psutil` (system metrics)
  and `defusedxml` (safe RSS/Atom parsing): `pip install -e ".[app]"`.
  `panel_driver` never imports them.
- **`dev`** — everything the test suite needs (`pytest`, `ruff`, `psutil`,
  `defusedxml`): `pip install -e ".[dev]"`.

### Linux: let your account open the panel (udev rule)

By default `hidraw` devices are owned by `root`, so a normal account cannot
open the panel. Install the bundled udev rule:

```bash
sudo cp packaging/99-smart-panel.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

The rule is exactly:

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="5548", ATTRS{idProduct}=="1011", TAG+="uaccess", GROUP="plugdev", MODE="0660"
```

- `TAG+="uaccess"` grants the user sitting at the seat (the interactive
  desktop/login user) access to the device.
- `GROUP="plugdev"` grants a **headless service account** access: add the
  account that runs the runner to the `plugdev` group
  (`sudo usermod -aG plugdev <service-account>`), then log out and back in (or
  reboot) for the group membership to take effect.
- `MODE="0660"` is deliberately **not** world-writable (`0666`). A world-
  writable device node would let any local account open the panel directly and
  bypass the CLI access policy (FR-051), which is enforced by the runner over
  the control channel, not by file permissions alone.

Replug the panel after reloading udev so the new permissions apply.

## Usage

The CLI is the display's only control surface; everything that affects the
display can be inspected and changed through it, with a machine-readable output
mode for agents.

```bash
smart-panel start                 # start the runner in the background
smart-panel --json status         # runner, panel, fps, plugin health, spool depth
smart-panel plugin list           # built-in and discovered plugins
smart-panel alert push --severity warning --title "Disk" --message "nas01 at 91%"
smart-panel preview --output /tmp/board.png   # render the canvas to a PNG (no panel needed)
smart-panel stop
```

Every command accepts `--json` and returns a stable `{"ok": true, "data": ...}`
/ `{"ok": false, "error": {...}}` envelope with documented exit codes, so an
agent can configure a complete display from an empty setup using CLI commands
alone. On a Raspberry Pi the runner can run unattended as a service
(`smart-panel service install`).

End-to-end validation scenarios (A–H without a panel, and the Raspberry Pi
hardware scenarios P1–P4) live in
[`specs/001-status-display-platform/quickstart.md`](specs/001-status-display-platform/quickstart.md).

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest -m "not hardware"     # `hardware`-marked tests need the physical panel and are skipped by default
```

Hardware tests must be skipped, not faked; no panel is available in CI.

## Contributing

`main` is **protected**: all changes arrive through a **pull request** — nothing
is pushed to `main` directly, force pushes and branch deletion are disabled, and
the required CI checks must pass. A pull request describes what changed and why,
states how it was tested (including hardware verification where relevant), and
records the constitution check.

The project constitution
([`.specify/memory/constitution.md`](.specify/memory/constitution.md)) governs
spec-driven work; its principles are the acceptance bar, not decoration.
Contributions must follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

MIT — see [LICENSE](LICENSE). Bundled third-party assets and their licenses are
recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
