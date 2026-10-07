# Implementation Plan: Status Display Platform

**Branch**: `001-status-display-platform` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-status-display-platform/spec.md`

## Summary

Turn the existing transport library (`panel_driver`: USB HID framing, init, rotation, one-shot frame loop) into a managed, always-on status display. A new application layer, `smart_panel`, adds:

- a **runner** that composes a 1920×462 canvas from region plugins, fits each frame to the panel's size limit, and pushes it through a pluggable **sink** (real panel, PNG files, or null), reconnecting automatically;
- a **plugin SDK** and loader (entry points plus a drop-in directory) with per-plugin isolation; the four requested features (alerts, crawl, gauges, sidebar) ship as built-in plugins using only that SDK, plus a `text` sample and a `sources` plugin that supplies data sources (system metrics, pushed values, JSON endpoints, RSS/Atom);
- a **CLI** (`smart-panel`) that is a thin client of a local Unix-socket control channel, with a stable `--json` envelope, documented exit codes, a self-describing `schema` command for agents, and a spool fallback so pushes still succeed while the runner is down;
- a durable **SQLite state store** (alert history, missed alerts, pushed values) and a JSON **config file** validated and applied live.

Technical approach: Python 3.11+, Pillow for rendering, stdlib for everything else on the hot path. Only two new runtime dependencies (`psutil`, `defusedxml`), both confined to the application layer and shipped as an optional `app` extra, so installing the transport alone pulls in only `hid` and `Pillow`. Delivery is staged to follow the spec's priorities, starting with a Phase 0 that fixes packaging and establishes pytest and CI, as the constitution requires.

## Technical Context

**Language/Version**: Python 3.11+ (Raspberry Pi OS 12 ships 3.11; developed on macOS with 3.13). `requires-python` moves from `>=3.10` to `>=3.11` in Phase 0.

**Primary Dependencies**: Existing: `hid`, `Pillow`. New (application layer only, `app` extra, also included in `dev`): `psutil` (system metrics), `defusedxml` (safe RSS/Atom parsing). Dev: `pytest`, `ruff`. Everything else is stdlib: `argparse`, `socket`, `sqlite3`, `threading`, `urllib`, `json`, `importlib.metadata`.

**Storage**: A JSON config file (atomic write, versioned), a SQLite database in WAL mode for runtime state (alert history, missed alerts, pushed values), a spool directory for pushes made while the runner is down, and a `0600` secrets file. All under one configurable home directory.

**Testing**: pytest. Layers: unit, contract (CLI JSON shapes, control protocol, plugin SDK conformance), integration (a runner started with the file sink and an injected fake clock, driven through the real CLI), and `hardware`-marked tests skipped by default. Golden fixtures for the wire protocol are extracted from `handoff/fullpaneltest.pcapng`.

**Target Platform**: Raspberry Pi 4B (4 GB) on Raspberry Pi OS 64-bit, alongside the Hermes agent; macOS for development. Linux needs a udev rule for hidraw access (`uaccess` plus `plugdev` group, not world-writable; documented in README in Phase 0).

**Project Type**: Library plus long-running daemon plus CLI (single Python distribution, two importable packages).

**Performance Goals**: Crawl at 10 fps or better with a default target of 15 fps; alert on screen within 2 s of push at p95; config change visible within 5 s; frame compose plus encode ≤ 40 ms p95 on a Pi 4B; steady state ≤ 50% of one core and ≤ 500 MB RSS (SC-005).

**Constraints**: Frame (header plus JPEG) ≤ 65,535 bytes, so quality adapts per frame; panel needs init each session and continuous refresh; USB write path is blocking and must not stall rendering; the agent shares the machine, so the runner runs niced and sheds load (FR-048). No panel is available in CI.

**Scale/Scope**: One panel, one runner; ≤ 6 gauges; sidebar ≤ 6 entries; alert queue default 50 (configurable 5 to 200); 6 built-in plugins; roughly 50 CLI subcommands; alert history bounded by the retention window.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle / section | Gate | Status |
|---|---|---|
| I. Evidence-based protocol fidelity | No change to wire protocol semantics is planned. New protocol knowledge (measured throughput) is added to the handoff doc with the capture analysis. Golden tests derived from the capture. Bytes 12-13 stay labeled unverified. | PASS |
| II. Hardware-safe by default | The sink sends CRTDIS+CRTLIG on every (re)open; brightness is validated 0-100 at the library boundary (Phase 0 hardening of `device.py`), and brightness `0` stays labeled unverified until the Phase 0 hardware smoke test (T129) records its effect; frame size is fitted before send; every `dev.write` keeps the `\x00` prefix and stays inside `device.py`; no new command bytes are introduced. | PASS |
| III. Layered and minimal | `panel_driver` stays a transport (base deps unchanged; `psutil` and `defusedxml` are an optional `app` extra). All rendering, plugins, CLI, and new dependencies live in `smart_panel`, which depends on `panel_driver`, never the reverse. New abstractions (sinks, plugin SDK, control channel) each trace to a spec requirement (FR-012, FR-043 to FR-047, FR-002/FR-004). | PASS |
| IV. Testable without hardware | File/null sinks and an injectable clock let the whole pipeline run under pytest. Hardware tests are marked and skipped by default. CI is created in Phase 0, before feature work. | PASS |
| V. Cross-platform and reproducible | macOS and Linux in the CI matrix; platform differences (peer credentials, service manager, hid quirk) isolated in single modules (`control/peercred.py`, `service.py`, `device.py`); `requires-python` matches CI; install steps and udev rule documented. | PASS |
| Compatibility & Legal | Bundled font and icons use permissive licenses recorded in `THIRD_PARTY_NOTICES.md`; no vendor software or brand logos (generic mail/calendar/chat glyphs only); the capture is already filtered; ESPN and Google News are used as personal, non-commercial feeds and flagged as such. | PASS |
| Contribution Workflow | `main` branch protection applied and recorded in Phase 0; Spec Kit flow followed; PRs report hardware verification (hardware smoke tasks T129 and T130 run before later phases build on the sink and runner); docs updated with behavior. | PASS |

**Post-design re-check (after Phase 1)**: PASS. The only additions beyond the minimum are justified in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-status-display-platform/
├── plan.md              # This file
├── research.md          # Phase 0 output: decisions, rationale, alternatives
├── data-model.md        # Phase 1 output: entities, validation, state machines
├── quickstart.md        # Phase 1 output: end-to-end validation guide
├── contracts/
│   ├── cli.md               # Commands, flags, exit codes, JSON envelope
│   ├── control-protocol.md  # Unix-socket JSON-lines protocol and spool format
│   ├── plugin-sdk.md        # Plugin manifest, lifecycle, render and command contracts
│   └── builtin-plugins.md   # Settings and commands of the built-in plugins
├── checklists/requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks, not created here)
```

### Source Code (repository root)

```text
panel_driver/                 # existing transport layer (kept minimal)
├── device.py                 # + input validation (brightness), no new commands
├── protocol.py
├── rotation.py
└── stream.py                 # legacy loop, kept for scripts; superseded by smart_panel

smart_panel/                  # new application layer
├── core/
│   ├── paths.py              # home/runtime/spool locations; SMART_PANEL_HOME, SMART_PANEL_RUNTIME (shared default /tmp/smart-panel)
│   ├── config.py             # schema, atomic load/save, migration, live apply
│   ├── settings.py           # SettingSpec: typed, ranged, enum settings + validation
│   ├── store.py              # core SQLite (topics, meta) + per-plugin ctx.db()/ctx.kv
│   ├── bus.py                # topics: latest value + freshness + subscribers
│   ├── layout.py             # Region, Overlay, default layout
│   ├── compositor.py         # layer cache, dirty tracking, overlay compositing
│   ├── workers.py            # per-plugin render workers, deadlines, health
│   ├── encoder.py            # rotate + JPEG quality ladder -> packets
│   ├── sinks.py              # PanelSink protocol; HidSink, FileSink, NullSink
│   ├── governor.py           # load shedding, fps/refresh scaling, nice
│   ├── runner.py             # lifecycle, threads, signals, single-instance lock
│   ├── logging.py            # JSON-lines rotating log, redaction, ring buffer
│   └── control/
│       ├── protocol.py       # request/response schema
│       ├── server.py         # socket server, command dispatch
│       ├── client.py         # CLI-side client with spool fallback
│       ├── access.py         # allow-list policy (pure, unit-testable)
│       └── peercred.py       # SO_PEERCRED / LOCAL_PEERCRED (platform-isolated)
├── sdk/                      # PUBLIC plugin API (stable, documented)
│   ├── plugin.py             # Plugin base, Manifest, RenderResult, errors
│   ├── settings.py           # re-exports Setting types
│   ├── context.py            # InstanceContext, FrameContext (fonts, clock, bus, store)
│   └── testing.py            # harness to render/test a plugin without a panel
├── plugins/
│   ├── text/                 # static text region (sample and Phase 1 content)
│   ├── sources/              # data sources: system, pushed, http_json, rss
│   ├── alerts/               # overlay, queue, history, missed-alert topic
│   ├── crawl/                # news-style ticker
│   ├── gauges/               # dial / bar / numeric / sparkline, aggregation
│   └── sidebar/              # icons + count chips + alert entry
├── cli/
│   ├── main.py               # argparse tree built from core + plugin manifests
│   ├── output.py             # JSON envelope, human formatting, exit codes
│   └── commands/             # runner, config, plugin, access, service, logs, preview
├── service.py                # systemd --user / launchd unit generation
└── assets/
    ├── fonts/                # bundled TTF (permissive license)
    └── icons/                # bundled generic app icons (PNG)

tests/
├── unit/                     # pure logic: protocol, encoder, settings, access, aggregation
├── contract/                 # CLI JSON shapes, control protocol, plugin SDK conformance
├── integration/              # runner + file sink + fake clock driven via CLI
├── hardware/                 # @pytest.mark.hardware, skipped by default
└── fixtures/                 # wire packets and JPEG extracted from the capture

scripts/                      # existing hardware scripts (kept; bench_throughput.py added in Phase 0)
packaging/                    # udev rule (99-smart-panel.rules), service templates
examples/plugins/clock.py     # sample drop-in third-party plugin (Phase 6)
.github/workflows/ci.yml      # created in Phase 0
```

**Structure Decision**: One distribution with two packages, `panel_driver` (transport) and `smart_panel` (application), matching the constitution's layering rule. The public plugin API is an isolated `smart_panel/sdk` subpackage so third-party plugins depend only on it. Built-in plugins live in `smart_panel/plugins/` and import only from `smart_panel.sdk`, which a contract test enforces (FR-043).

## Delivery Phases

These are delivery milestones for implementation (distinct from the Spec Kit research and design phases above). Each ends with a green CI run. `/speckit-tasks` will expand them.

| Phase | Scope | Spec coverage |
|---|---|---|
| **0. Foundations and CI** | Fix `pyproject.toml` (valid build backend, Python `>=3.11`, package list incl. `smart_panel`, entry point `smart-panel`, `dev` extra with pytest and ruff). pytest scaffold. Golden wire-protocol tests from capture fixtures. Brightness validation in `device.py`. GitHub Actions (ubuntu and macos, Python 3.11 and 3.13, ruff, pytest, hardware tests skipped). README with install, udev rule, hardware notes. `THIRD_PARTY_NOTICES.md`. USB throughput benchmark script. Branch protection on `main` (PR required, CI checks required, no force push, applies to admins) via a recorded `scripts/protect_main.sh`. | Constitution IV and V; SC-010 |
| **1. Runner, CLI, plugin core** | Paths, config, settings, store, bus, layout, compositor, workers, encoder, sinks (hid/file/null), runner lifecycle and single-instance lock, control server/client/access, CLI core commands, SDK and loader, `text` plugin, `service install`, governor, status/logs. | US1, US6 (core), FR-001 to FR-016, FR-048 to FR-051 |
| **2. Alerts** | `alerts` plugin: queue, severities, preemption, timeouts, history, missed-alert tracking, spool replay; CLI `alert` verbs; overlay compositing of the main region. | US2, FR-017 to FR-024 |
| **3. Crawl and sources** | `sources` plugin (pushed, http_json, rss); `crawl` plugin with scrolling cache, ESPN and Google News presets. | US3, FR-025 to FR-030 |
| **4. Gauges** | `sources` system metrics; `gauges` plugin: aggregation windows, thresholds, four styles, staleness. | US4, FR-031 to FR-036 |
| **5. Sidebar** | `sidebar` plugin: icons, chips, hide-when-zero, abbreviation, stale state, built-in alert entry bound to the alerts topic. | US5, FR-037 to FR-042 |
| **6. Plugin authoring** | SDK docs, `sdk.testing` harness, sample third-party plugin with contract tests, failure-isolation tests. | US6, FR-043 to FR-047 |
| **7. Pi hardening** | Soak test (72 h), memory and CPU measurement on a Pi 4B alongside a load stand-in for the agent, panel unplug/replug, power-cycle runbook, handoff-doc update. | SC-001 to SC-009 |

## Complexity Tracking

| Addition | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Local control channel (Unix socket) plus a separate CLI process | FR-002/FR-007: change settings and push events into a running process with live effect, no restart. | Signal-plus-file reload cannot return validation errors, status, or per-command results to an agent. |
| SQLite state store (stdlib) | FR-018/FR-022/FR-024: alert history and the missed-alert count must survive restarts and be written by more than one process. | JSON files would need hand-written locking, pruning, and crash safety for concurrent writers. |
| Spool directory for pushes while runner is down | FR-005/FR-007/FR-018 (clarified): pushes must be saved while the runner is down and replayed on start. | Making pushes fail would force every caller to implement retry; writing straight into SQLite would bypass the access policy. |
| Manifest-driven CLI subcommands | FR-043: the core may not contain feature-specific logic, yet `alert push` and `crawl items set` must exist. | Hard-coding feature verbs in the CLI would put feature logic in the core and break plugin parity. |
| In-process plugin isolation (worker threads with deadlines) instead of subprocesses | FR-045 with the Pi's RAM budget. | A process per plugin multiplies Python and Pillow memory several times on a 4 GB machine shared with an agent. Limits of this choice are recorded in research.md. |
