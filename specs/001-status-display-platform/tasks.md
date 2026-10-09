---

description: "Task list for the Status Display Platform feature"
---

# Tasks: Status Display Platform

**Input**: Design documents from `/specs/001-status-display-platform/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Included. The constitution (Principle IV) requires pytest tests for pure logic that run without a panel, and SC-010 requires automated verification. Within each story, write the tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story. Phase 1 is the plan's "Phase 0 (Foundations and CI)" and must be green in CI before any feature work merges (constitution). Hardware verification (Raspberry Pi 4B) is in the final phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story the task belongs to (US1 to US6); Setup, Foundational, and Polish tasks have none
- Every task names exact file paths. Setting constraints are quoted verbatim from `data-model.md` so nothing is left to implementation-time discretion.

## Path Conventions

Single distribution with two packages: `panel_driver/` (transport, existing) and `smart_panel/` (application layer, new). Tests under `tests/{unit,contract,integration,hardware,fixtures}/`. Built-in plugins under `smart_panel/plugins/<name>/` and import only from `smart_panel.sdk`.

---

## Phase 1: Setup (Foundations and CI)

**Purpose**: Valid packaging, test scaffolding, CI, and the protocol safety net before any new code.

- [x] T001 Fix `pyproject.toml`: set `build-backend = "setuptools.build_meta"` (with `requires = ["setuptools>=64"]`), set `requires-python = ">=3.11"`, include packages `panel_driver*` and `smart_panel*` (plus package data `smart_panel/assets/**`), keep base dependencies at `hid>=1.0` and `Pillow>=10` (transport stays minimal, constitution III), add an `app` extra `["psutil", "defusedxml"]` for the application layer (`panel_driver` never imports them; `smart_panel` imports them lazily and the CLI prints an install hint if the extra is missing), define the `dev` extra as `["pytest", "ruff", "psutil", "defusedxml"]` so `pip install -e ".[dev]"` runs everything, replace the broken `send-test-pattern` script entry with `smart-panel = "smart_panel.cli.main:main"`, and register the pytest marker `hardware` under `[tool.pytest.ini_options]` with `addopts = "-m 'not hardware'"`.
- [x] T002 Create the package skeleton with empty `__init__.py` files: `smart_panel/__init__.py` (with `__version__ = "0.1.0"`), `smart_panel/core/`, `smart_panel/sdk/`, `smart_panel/plugins/`, `smart_panel/cli/`, `smart_panel/cli/commands/`, `smart_panel/assets/fonts/`, `smart_panel/assets/icons/`.
- [x] T003 [P] Create the test scaffold: `tests/__init__.py`, `tests/conftest.py` (fixture `tmp_home` that sets `SMART_PANEL_HOME` to a temp dir and a fixture `fake_clock`), and empty `tests/unit/`, `tests/contract/`, `tests/integration/`, `tests/hardware/`, `tests/fixtures/` directories with `__init__.py` where needed.
- [x] T004 [P] Write `scripts/extract_fixtures.py` that reads `handoff/fullpaneltest.pcapng` with `tshark` (field `usbhid.data`, filter `usb.endpoint_address==0x01 && usb.data_len>0`) and writes `tests/fixtures/crtdis_packet.bin`, `tests/fixtures/crtlig_packet.bin` (brightness 50) and `tests/fixtures/crtdra_first_packet.bin`; also copy `handoff/extracted_frame_from_capture.jpg` to `tests/fixtures/frame_58697.jpg`. Run it and commit the generated fixtures.
- [x] T005 [P] Write golden protocol tests in `tests/unit/test_protocol.py` against the fixtures: header bytes 0-9 are `CRT\0\0` + command, bytes 12-13 are `b1 00` for DRA and `00 00` for DIS and LIG, a synthetic 14,547-byte payload gives exactly 15 packets, the real `frame_58697.jpg` fixture (58,697 bytes) gives exactly 58 packets, every packet is 1024 bytes, and `build_frame_packets` raises `ValueError` when 32 + len(jpeg) exceeds 65535.
- [x] T006 [P] Write `tests/unit/test_rotation.py`: a 1920x462 image becomes 462x1920 with `clockwise=True` (`Image.ROTATE_270`), pixel at (0,0) lands where expected, and `clockwise=False` is the inverse.
- [x] T007 Harden `panel_driver/device.py` and reconcile the init-command byte order: move `import hid` inside `open_device()` so importing the module needs no hidapi library, validate brightness as an int in 0..100 in `init_display()` and `set_backlight()` (raise `ValueError` otherwise), and make `build_backlight_packet`/`build_init_packets` in `panel_driver/protocol.py` raise `ValueError` outside 0..100. **Also correct `build_init_packets()` to reproduce the vendor capture byte-for-byte:** CRTDIS carries `\x00\x00` in header bytes 10-11 (not the preamble length 32), and CRTLIG encodes brightness **little-endian** (`\x32\x00` = 50, not big-endian `\x00\x32`); CRTDRA stays big-endian and is already correct. This is the reconciliation PR #7 routed here and PR #5 left unfixed. Add a **builder-vs-fixture regression test** asserting `build_init_packets(50)` reproduces the captured CRTDIS/CRTLIG packets byte-for-byte (the existing golden tests only pin the fixtures, never the builder — which is why the defect passed review). Record the new validation rules and the corrected byte order in `handoff/panel_protocol_handoff.md` in the same PR (constitution I). Add `tests/unit/test_device_validation.py` using a fake device object that records writes and asserts every write is `b"\x00" + 1024 bytes` (1025 bytes).
- [x] T008 [P] Configure ruff in `pyproject.toml` (`[tool.ruff]`, line length 100, select `E,F,I,UP,B`) and fix findings in `panel_driver/` and `scripts/`.
- [x] T009 [P] Create `.github/workflows/ci.yml`: matrix `ubuntu-latest` and `macos-latest` x Python `3.11` and `3.13`; steps checkout, setup-python, install hidapi (`sudo apt-get install -y libhidapi-hidraw0` on Ubuntu, `brew install hidapi` on macOS), `pip install -e ".[dev]"`, `ruff check .`, `pytest -m "not hardware"`.
- [x] T010 [P] Write `README.md` (project purpose, supported hardware `0x5548:0x1011`, install steps, the Linux udev rule, the quickstart pointer, license, constitution link) and `packaging/99-smart-panel.rules` with `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="5548", ATTRS{idProduct}=="1011", TAG+="uaccess", GROUP="plugdev", MODE="0660"` (seat user via `uaccess`, headless service accounts via the `plugdev` group; not world-writable, so other accounts cannot bypass the CLI access policy). The README also states that `main` is protected and all changes arrive by pull request. The README documents the `app` extra and adding the service account to `plugdev`.
- [x] T011 [P] Create `THIRD_PARTY_NOTICES.md` listing DejaVu Fonts (Bitstream Vera-derived license) with its license text and the files it covers; the Tabler Icons (MIT) entry is added by T105 when the icons are bundled.
- [x] T012 [P] Write `scripts/bench_throughput.py`: open the panel, send the test pattern for N seconds with no sleep, and report packets per second, per-frame write time (median and p95), and sustainable fps; accept `--seconds`, `--quality`. Mark it as hardware-only in its docstring.
- [x] T013 [P] Update `handoff/panel_protocol_handoff.md` section 2 "Streaming behavior" with the capture analysis: 340 CRTDRA frames in 19.3 s (17.6 fps), median frame interval 61 ms, 58-packet frames transferring in a median 13 ms (about 0.23 ms per packet), noting it was measured on Windows and replacing the "12ms per packet-then-ack pair" statement.
- [x] T014 Update the Commands section of `CLAUDE.md` for the working setup (`pip install -e ".[dev]"`, `pytest`, `ruff check .`) and remove the "fails until fixed" notes (depends on T001).
- [ ] T133 [P] Branch protection on `main` (constitution, Contribution Workflow): write `scripts/protect_main.sh` that calls `gh api -X PUT repos/{owner}/{repo}/branches/main/protection` to require a pull request before merging (zero required approvals while there is a single maintainer), require the CI status checks from T009 (job names must match the first CI run), enforce the rules for administrators, and disable force pushes and branch deletion; add `docs/branch-protection.md` describing the settings and how to verify them with `gh api repos/{owner}/{repo}/branches/main/protection`. The repository owner runs the script after the first green CI run (depends on T009); applying it is an outward-facing change to the remote repository, so it needs the owner's explicit go-ahead.
- [x] T129 Hardware smoke test for the Phase 1 changes to `panel_driver/` (constitution IV): on the real panel, run `scripts/send_test_pattern.py` and `scripts/stream.py` for at least 10 minutes after the T007 changes, and run `set_backlight` at 0, 50 and 100. Record the hardware, OS, duration and observed result of each brightness value in `handoff/panel_protocol_handoff.md` (verified or explicitly labeled unverified, Principle I) and in the PR description (depends on T007).
- [ ] T015 Checkpoint task: in `.venv`, run `pip install -e ".[dev]"`, `ruff check .`, and `pytest`; all must pass, and the CI workflow must be green on a pushed branch (depends on T001 to T014 and T129). Branch protection (T133) is applied by the owner after the first green CI run, which happens after the Phase 1 PR merges; verifying that protection is active on `main` gates the start of Phase 2, not the merge of the Phase 1 PR.

**Checkpoint**: Packaging valid, tests and CI green on Linux and macOS, protocol covered by golden tests. Feature work may begin.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The SDK, state, config, rendering, and sink infrastructure every user story depends on. No user story work can begin until this phase is complete.

> **Note (Phase 1):** T008 (ruff config) and T009 (CI) **both edit `pyproject.toml`** and T009 also authors `.github/workflows/ci.yml`. They MUST run as **one serial card** with a single writer — splitting them across workers collides in `pyproject.toml`. T009's required check names are consumed verbatim by T133 (branch protection), so T009 lands before T133.

- [ ] T016 [P] Implement `smart_panel/core/paths.py`: `home()` = `$SMART_PANEL_HOME` or `~/.config/smart-panel` (created mode 0700), `runtime()` = `$SMART_PANEL_RUNTIME`, else `/tmp/smart-panel` (the same path for every account; mode 0755; only the runner and `service install` create it, the CLI never does; the runner verifies it is owned by its own uid and refuses to start otherwise, and `pending_spool()` = `<home>/spool-pending` for the owner's fallback), `spool()` = `<runtime>/spool` (sticky mode 1733) unless `paths.spool` overrides it, plus helpers for `config.json`, `secrets.json`, `state.db`, `state/`, `plugins/`, `control.sock`, `runner.lock`. Test in `tests/unit/test_paths.py` (modes, env overrides, spool override, same default runtime path for different uids, refusal of a runtime directory owned by another uid, and that no CLI-side helper creates the runtime directory).
- [ ] T017 [P] Implement `smart_panel/sdk/settings.py`: `Setting(name, type, default, description, min, max, choices, pattern, secret, item)` with types `int`, `float`, `bool`, `str`, `color`, `enum`, `list`, `object`, `duration`; `validate(settings_specs, values)` returns a conforming dict or raises `SettingError(path, message)`; `color` accepts `#RRGGBB`, `#RRGGBBAA`; secret settings accept only `${secret:NAME}`. Test in `tests/unit/test_settings.py` (ranges, enums, unknown keys rejected naming the dotted path, color parsing, secret references).
- [ ] T018 [P] Implement `smart_panel/sdk/plugin.py` per `contracts/plugin-sdk.md`: `Manifest` (name, version, `sdk_version`, summary, capabilities (`region`, `overlay`, `datasource`, `alert_source`, `alert_sink`), settings, commands, source_types, animated, min_size, max_size), `Command(name, summary, args, spoolable, mutates_config)`, `SourceType`, `Plugin` base class with lifecycle methods (`start`, `stop`, `on_settings_changed`, `render`, `handle_command`, `describe_state`), `RenderResult` variants `Unchanged`, `Image`, `Inactive`, and exceptions `SettingsRejected`, `CommandError(code, message)`.
- [ ] T019 [P] Implement `smart_panel/sdk/context.py`: `InstanceContext` (name, region_size, log, bus, kv, `db()`, `spawn`, `http`, clock, `request_render`, `notify`, `alert(severity, message, title=None, source=None, id=None, ttl=None)` (allowed only for manifests declaring `alert_source`; routed generically by the core to the instance declaring `alert_sink`, so the core holds no alerts logic), `FrameContext` (size, now, wall, frame_index, fonts, icons, bus read-only, min_text_px), `Sample(value, ts, freshness)` and the `Clock` protocol with `monotonic()` and `time()`.
- [ ] T020 [P] Implement `smart_panel/core/bus.py`: topic store with `publish(topic, value, ttl_s=None)`, `get(topic)` returning a `Sample` whose freshness is `fresh` (age <= ttl), `stale` (age > ttl) or `no_data` (never set), `subscribe(topic, callback)`, `topics(prefix)`; thread-safe; injectable clock. Test in `tests/unit/test_bus.py` (freshness transitions with the fake clock, subscriptions, prefix listing).
- [ ] T021 Implement `smart_panel/core/store.py`: core `state.db` in WAL mode with tables `topics(topic PK, value_json, ts, ttl_s)` and `meta(key PK, value)`, persistence of pushed topics, `ctx.kv` (namespaced JSON values, "<= 64 KiB each"), and `ctx.db()` returning a private stdlib `sqlite3` connection to `<home>/state/<instance>.db` in WAL mode. Test in `tests/unit/test_store.py` (persistence across reopen, 64 KiB limit, per-instance DB isolation) (depends on T016).
- [ ] T022 [P] Implement `smart_panel/core/logging.py`: JSON-lines rotating log (`RotatingFileHandler`, 1 MB x 5) at `<home>/runner.log`, an in-memory ring of the last 200 events, and a redaction filter that masks any value registered as a secret. Test in `tests/unit/test_logging.py` (rotation limits, ring size, secrets never appear).
- [ ] T023 [P] Implement `smart_panel/core/secrets.py`: `secrets.json` stored with mode 0600, `set/delete/list_names`, and `resolve("${secret:NAME}")`; registers resolved values with the logging redaction filter. Test in `tests/unit/test_secrets.py` (file mode, resolution, missing name error) (depends on T016, T022).
- [ ] T024 Implement `smart_panel/core/config.py` per `data-model.md`: JSON `config.json` with `"version": 1` and a migration hook; sections `display` (`fps` int "5..30, default 15", `brightness` int "0..100, default 50", `jpeg_quality` `{start: int 30..95 = 70, min: int 20..start = 30}`, `min_text_px` int "12..64, default 24", `sink` one of `"hid" | "files" | "null"` default `"hid"`, `files_dir` path or null), `access` (`allowed_users` list default `[]`, `allowed_group` str or null), `paths` (`spool`), `layout`, `plugins`, `sources`; unknown keys rejected with the dotted path of the offender; atomic write (temp file plus rename); dotted-path `get/set/unset` that validate the whole candidate copy and leave the live config untouched on error. Test in `tests/unit/test_config.py` (defaults, each range edge, unknown key message, atomic write survives a simulated crash, set/unset round trip) (depends on T016, T017).
- [ ] T025 [P] Implement `smart_panel/core/layout.py`: `Region(name, x, y, w, h, instance)` and `Overlay(region, instance, z=0)`, the fixed 1920x462 canvas, and the default layout (sidebar 0,0,192,462; main 192,0,1728,382; crawl 192,382,1728,80; overlay `alerts` on `main`), kept in `smart_panel/core/defaults.py`, the only core module that names built-in plugins (FR-015). Validation: "Names unique; rectangles inside the canvas; regions may not overlap", minimum 64 px per side, overlay targets an existing region. Test in `tests/unit/test_layout.py` (default layout valid, each rule violated gives a message naming the region).
- [ ] T026 [P] Implement `smart_panel/core/encoder.py`: rotate with `panel_driver.rotation.to_panel_native(clockwise=True)`, JPEG-encode with the quality ladder 70, 60, 50, 40, 30 starting from the last quality that fit, require "32 + len(jpeg) <= 65535" (JPEG at most 65,503 bytes), fall back to the next rung on overflow, raise `FrameTooLarge` if the lowest rung still overflows, and step quality back up after a streak of small frames; return packets from `panel_driver.protocol.build_frame_packets`. Test in `tests/unit/test_encoder.py` with synthetic flat and noisy 1920x462 images (flat fits at 70; noise forces lower rungs; impossible case raises).
- [ ] T027 [P] Implement `smart_panel/core/sinks.py` part 1: the `PanelSink` protocol (`open()`, `send(packets)`, `set_brightness(n)`, `close()`, `state()` returning connected/disconnected/unresponsive plus `last_error`), `FileSink(dir)` writing `latest.png` atomically (and numbered frames when asked) from the composed canvas, and `NullSink`. Test in `tests/unit/test_sinks_file_null.py`.
- [ ] T028 Implement `HidSink` in `smart_panel/core/sinks.py`: opens via `panel_driver.device.open_device`, retries with backoff 1, 2, 4, 8, 10 s (cap), sends CRTDIS + CRTLIG on every successful open and reapplies brightness, counts consecutive write failures and reopens, reports `panel_unresponsive` with a power-cycle hint after more than 60 s of continuous write failures, and keeps every `dev.write` inside `panel_driver/device.py`. **Hardware-measured policy (T129, 2026-10-09): the panel intermittently stops accepting OUT reports after 30–600 frames with NO USB disconnect and independent of rate and byte order, and reopening the handle recovers it on the FIRST attempt every time (22/22 in a 600 s soak, ~8.7 fps effective). So the reopen must trigger on the FIRST write failure, not after 3** — a 3-failure threshold would burn the stall window on retries that cannot succeed on the dead handle. The `panel_unresponsive` power-cycle hint applies only to the *open* failing repeatedly, not to a mid-stream stall. Test in `tests/unit/test_hid_sink.py` with a fake device and fake clock (init sent on each reopen, backoff sequence on repeated open failure, **reopen on the first mid-stream write failure**, and the unresponsive threshold) (depends on T007, T027).
- [ ] T029 Implement `smart_panel/core/workers.py`: one render worker thread per plugin instance with latest-result slot, soft deadline 250 ms (logged) and hard deadline 2 s (marks the instance `stalled`, keeps the last good image, does not call it again until it returns), instance states `starting`, `ok`, `degraded`, `stalled`, `failed`, `disabled`, invalid output (wrong type or size, exception) keeps the previous image, marks `degraded` and records `last_error`. Test in `tests/unit/test_workers.py` using plugin stubs that raise, return the wrong size, and sleep past deadlines (depends on T018, T019).
- [ ] T030 Implement `smart_panel/core/compositor.py`: keeps a cached RGBA layer per region, composes the 1920x462 canvas by pasting region layers then overlays (an overlay is drawn over its whole target region only while it returns an image; `Inactive` draws nothing; `Unchanged` reuses the cache), tracks dirty regions, and draws a small error badge in a region corner for `degraded` or `stalled` instances. Test in `tests/unit/test_compositor.py` (overlay covers only its region, sidebar and crawl untouched, unchanged results are cheap, error badge appears) (depends on T025, T029).
- [ ] T031 [P] Implement `smart_panel/sdk/testing.py`: `FakeClock`, `PluginHarness(plugin_class, settings, size, clock)` with `.render(advance_s=0)`, `.command(name, **args)`, `.bus`, `.set_settings(...)`, `.state()`, and `assert_conforms(plugin_class)` (manifest valid, settings validate, commands declared, render returns allowed types at the exact size, `stop()` returns within 2 s, no imports from `smart_panel.core`). Test the harness itself in `tests/contract/test_sdk_testing.py` with a tiny sample plugin (depends on T017, T018, T019).
- [ ] T032 Implement `smart_panel/core/plugins.py` (loader): discover via entry-point group `smart_panel.plugins` and via `*.py` files or package directories in `<home>/plugins/`; plugin names must match `[a-z][a-z0-9_-]{1,31}`, be unique, and a drop-in may not shadow a built-in; import errors are caught and reported as `load_failed` with the message; plugins targeting a newer `sdk_version` major are listed `incompatible`; expose a `plugins_rev` counter that increments when the set changes. Test in `tests/unit/test_plugin_loader.py` with temp drop-in files (valid, broken import, name clash, bad name) (depends on T018, T016).
- [ ] T033 [P] Write `tests/contract/test_builtin_imports.py`: an AST scan that fails if any module under `smart_panel/plugins/` imports from `smart_panel.core` or any module other than `smart_panel.sdk`, the standard library, `PIL`, `psutil`, or `defusedxml` (FR-043).
- [ ] T034 [P] Bundle fonts and implement `smart_panel/core/fonts.py`: add DejaVu Sans and DejaVu Sans Bold TTFs to `smart_panel/assets/fonts/`, resolve a font by bundled name or TTF path with a configurable fallback chain, cache `ImageFont` objects by (path, size), and draw the font's replacement box for a missing glyph without raising; a missing font file falls back along the chain and raises a `ctx.notify`-style warning that `status` lists. Test in `tests/unit/test_fonts.py` (name and path lookup, size cache, missing glyph, emoji/CJK strings do not raise).
- [ ] T035 [P] Implement `smart_panel/core/scheduler.py`: a thread pool (max 4 workers) for `ctx.spawn` tasks and data-source fetches with per-task timeouts, one in-flight run per task, cancellation on `stop`, and an interval multiplier the governor can raise. Test in `tests/unit/test_scheduler.py` with the fake clock (no overlap of a task with itself, timeout, cancel, multiplier).

**Checkpoint**: SDK, config, bus, store, layout, compositor, encoder, sinks, loader, fonts, and scheduler exist and are tested. The runner and CLI can now be built on top, and all stories can proceed.

---

## Phase 3: User Story 1 - Run and manage the display from the command line (Priority: P1) 🎯 MVP

**Goal**: A long-running runner drives the panel (or a file/null sink), and a `smart-panel` CLI starts, stops, restarts, reloads, inspects, and configures it, with machine-readable output, live config changes, panel loss recovery, spooled pushes, and access control.

**Independent Test**: Quickstart Scenarios A, C (generic part), and H (policy). With the file sink, `start` shows a configured `text` instance in `preview` within 5 s of a `config set`; `start` twice gives exit 6; an invalid `config set` gives exit 2 and changes nothing; `restart` returns content within 10 s; unplug/replug (fake device) keeps the runner alive and resumes frames.

### Tests for User Story 1 (write first, confirm they fail)

- [ ] T036 [P] [US1] Contract test for the CLI envelope in `tests/contract/test_cli_envelope.py`: success is `{"ok": true, "data": ...}`, failure is `{"ok": false, "error": {"code", "message", "setting"?}}` on stdout, argument errors also use the envelope, and exit codes are exactly 0 ok, 1 error, 2 invalid_input, 3 runner_not_running, 4 panel_unavailable, 5 not_permitted, 6 conflict, 7 plugin_error.
- [ ] T037 [P] [US1] Unit test for the access policy in `tests/unit/test_access.py`: allowed for the runner's uid and uid 0, for any uid in `access.allowed_users`, and for any member of `access.allowed_group`; denied otherwise; default policy allows no additional accounts; `requires_owner` permits `access.*` mutations and `secret.*` only for the runner's uid or root (an allowed non-owner account is refused with `not_permitted`).
- [ ] T038 [P] [US1] Contract test for the control protocol in `tests/contract/test_control_protocol.py`: first line must be the `hello` with `"v": 1`, major mismatch returns `protocol_mismatch` and closes, request ids are echoed, lines over 1 MiB are rejected, error codes are drawn only from the documented set.
- [ ] T039 [P] [US1] Integration test for lifecycle in `tests/integration/test_runner_lifecycle.py` (file sink, fake clock, real CLI via subprocess): start, status fields from `contracts/cli.md`, second start exits 6, `config set display.fps 500` exits 2 naming `display.fps` and leaves the running config unchanged, `config set` of a text instance appears in `preview` within 5 s with no blank or partial frame in the `FileSink` frame sequence across the change (US1 scenario 2), `restart`, `reload` with a broken file keeps the last good config, `stop`.
- [ ] T040 [P] [US1] Integration test for spool in `tests/integration/test_spool.py`: with the runner stopped, `text set` (a spoolable verb) exits 0 with `saved_for_later: true`; `status` exits 3; on `start` the spool replays in receive-time order; files owned by a non-allowed uid are rejected and deleted; malformed or over 64 KiB files are rejected; the 1,000-file cap returns `spool_full`; a second allowed uid (fake peer credentials) reaches the same socket and spool path as the owner; with the runtime directory missing, the owner's push goes to `<home>/spool-pending` (imported by the runner on start, same rules) and an allowed non-owner account (identified by `$SMART_PANEL_RUNNER_UID` differing from its uid) gets exit 3 instead of creating the directory; a pre-created runtime directory owned by another uid makes the runner refuse to start with a clear message; a socket or directory owned by a foreign uid not in `{self, root, $SMART_PANEL_RUNNER_UID}` is refused by the CLI (impersonation guard).
- [ ] T041 [P] [US1] Unit test for the governor in `tests/unit/test_governor.py`: three consecutive overrun windows step fps 15, 12, 10, 8, 5 and double source intervals (capped at 4x); 60 s of headroom steps back up; each change is logged and visible in the reported level.
- [ ] T042 [P] [US1] Integration test for panel loss in `tests/integration/test_panel_loss.py`: the runner starts with no device (reports "waiting for panel"), a fake device appears and frames start, the device then fails writes and the runner reconnects with init resent, and the runner process never exits.

### Implementation for User Story 1

- [ ] T043 [P] [US1] Implement the `text` plugin in `smart_panel/plugins/text/__init__.py`: capabilities `region`; settings `text` (str, "<= 280 chars", default `"smart-panel"`), `fg` (color `#FFFFFF`), `bg` (color `#000000`), `font` (default `"DejaVuSans"`), `size` (int, default 48, "must stay at or above `frame.min_text_px`"), `align` (enum `left|center|right`, default `center`); command `set --text T` marked `spoolable`; passes `assert_conforms` (depends on T018, T031, T034).
- [ ] T044 [US1] Implement `smart_panel/core/control/protocol.py`: request/response/`hello` dataclasses and JSON-lines encode/decode, error code set (`invalid_input`, `not_found`, `not_permitted`, `conflict`, `panel_unavailable`, `plugin_error`, `spool_full`, `timeout`, `protocol_mismatch`, `internal`), 1 MiB line limit, and the code-to-exit-code mapping used by the CLI.
- [ ] T045 [P] [US1] Implement `smart_panel/core/control/access.py`: pure `is_allowed(peer_uid, peer_groups, policy)` and `requires_owner(cmd)` for `access.*` mutations and `secret.*` commands (depends on T037 tests).
- [ ] T046 [P] [US1] Implement `smart_panel/core/control/peercred.py`: `get_peer(conn) -> (uid, gid)` using `SO_PEERCRED` on Linux and `LOCAL_PEERCRED` (`SOL_LOCAL`) on macOS, plus supplementary groups via `os.getgrouplist`; all platform-specific code stays in this file.
- [ ] T047 [US1] Implement `smart_panel/core/control/server.py`: Unix socket at `<runtime>/control.sock` (shared default `/tmp/smart-panel`, directory 0755, socket 0666), per-connection authorization via `peercred` and `access` (denied peers get one `not_permitted` response, a logged `denied uid=<n>`, then close), one serialized command queue across connections, 2 s deadline for mutating plugin verbs, `logs.tail` follow streaming (depends on T044, T045, T046).
- [ ] T048 [US1] Implement `smart_panel/core/control/client.py`: connect with timeout to `<runtime>/control.sock` (same path for all accounts; refuse a runtime directory or socket not owned by the invoking uid, root, or the uid in `$SMART_PANEL_RUNNER_UID`, so another account cannot impersonate the runner), `hello` handshake, send/receive, and the spool fallback used only for verbs whose manifest marks them `spoolable` when connect fails with `ENOENT` or `ECONNREFUSED`: if `<runtime>/spool` exists the file goes there, otherwise (the CLI never creates the runtime directory) it goes to `<home>/spool-pending` for the owner's account, or the command fails with exit 3 for an account whose uid differs from `$SMART_PANEL_RUNNER_UID`; spool files `<received_at_ns>-<uuid>.json` written as `.tmp` then renamed, mode 0644, content `{"v": 1, "received_at": ..., "cmd": ..., "args": ...}`; at 1,000 files return `spool_full` (depends on T044).
- [ ] T049 [US1] Implement `smart_panel/core/control/spool.py` (runner-side ingest): list in name order, also ingest the runner's own `<home>/spool-pending`, reject files whose owner uid is not allowed (delete and log), reject malformed or over 64 KiB files, replay each accepted command through the normal command path with `received_at` preserved, delete processed files, expose the pending count for `status` (depends on T045, T047).
- [ ] T050 [P] [US1] Implement `smart_panel/core/governor.py`: call `os.nice(5)` at start, measure frame time and process CPU every 5 s, step fps down (15, 12, 10, 8, 5) after 3 consecutive overrun windows and raise the scheduler's interval multiplier up to 4x, step back up after 60 s of headroom, and log each change (depends on T035).
- [ ] T051 [US1] Implement `smart_panel/core/runner.py`: single-instance rule with `flock` on `<runtime>/runner.lock` plus `runner.pid` (second runner exits with conflict), thread layout from `research.md` decision 3 (compositor loop at fps, per-plugin workers, sender thread with a single-slot latest-frame-wins buffer, control server, scheduler, housekeeping), graceful stop on SIGTERM/SIGINT, a test-only `--clock-file PATH` option (honored only when `SMART_PANEL_TESTING=1`, never reachable on default paths) so subprocess integration tests can advance a fake clock by writing the file, start with no panel attached, spool ingest before the first frame, live config apply (instance settings change calls `on_settings_changed`, layout change rebuilds the compositor, instance add or remove starts or stops plugins, rejects with the previous config kept), brightness applied through the sink, and `status` aggregation (state, uptime, pid, version, panel, frames fps/target/governor level/last_bytes/quality, layout, plugin states, spool pending, recent errors) (depends on T024 to T030, T032, T035, T047, T049, T050).
- [ ] T052 [US1] Implement `smart_panel/core/commands.py`: control command handlers for `runner.status|reload|stop`, `config.get|show|set|unset|validate|schema`, `secret.set|delete|list`, `display.brightness.set|get`, `display.preview` (PNG as base64), `layout.*`, `plugin.list|describe|instance.*`, `plugin.call` (routes to plugin `handle_command`, mapping `CommandError` to `plugin_error`), `access.*`, `logs.tail`, `schema.get` (depends on T051).
- [ ] T053 [P] [US1] Implement `smart_panel/cli/output.py`: JSON envelope writer, human-readable formatter, `--json`/`SMART_PANEL_JSON`/`--quiet` handling, and the error-code to exit-code table.
- [ ] T054 [US1] Implement `smart_panel/cli/main.py`: global flags `--home`, `--json`, `--quiet`, `--timeout`; subcommand tree built from core commands plus plugin verb groups generated from plugin manifests, cached in `<home>/cli-cache.json` and refreshed when the server's `plugins_rev` changes or on `--refresh`; when the cache is missing or stale and the runner is down, the CLI builds the tree directly from the plugin loader so spoolable verbs work on a cold start; argument errors exit 2 using the envelope (depends on T044, T048, T053).
- [ ] T055 [P] [US1] Implement `smart_panel/cli/commands/runner.py`: `run`, `start` (detached via `subprocess.Popen(start_new_session=True)`, waits for the socket and first frame or the timeout, exit 6 if running), `stop`, `restart`, `reload`, `status`; delegates to the service manager when a service is installed.
- [ ] T056 [P] [US1] Implement `smart_panel/cli/commands/config.py`: `config show|get|set|unset|validate|schema|path` (values parsed as JSON, else string; secrets shown as `"***"`), and `secret set NAME [--stdin]|list|delete`; works against the config file directly when the runner is down and the caller owns it.
- [ ] T057 [P] [US1] Implement `smart_panel/cli/commands/display.py`: `brightness set|get` (0-100; `0` is documented as "backlight off, unverified on hardware" in help and docs until T129 records the result, and if T129 shows `0` is unsafe or does not blank the panel the lower bound becomes 1 and FR-011 is amended), `layout show|set --file|region set|overlay add|overlay remove`, and `preview [--output FILE.png]`.
- [ ] T058 [P] [US1] Implement `smart_panel/cli/commands/plugins.py`: `plugin list|describe|instance list|add|set|enable|disable|remove|call`, including `--set K=V` and `--arg K=V`/`--json-args` parsing validated against the manifest.
- [ ] T059 [P] [US1] Implement `smart_panel/cli/commands/misc.py`: `access list|allow-user|deny-user|set-group|clear-group`, `logs [--follow] [--lines N] [--level L]`, `schema` (one JSON document of every command, argument, exit code, config key, and plugin verb), and `version`.
- [ ] T060 [P] [US1] Implement `smart_panel/service.py` and `smart_panel/cli/commands/service.py`: generate and install a systemd user unit (`~/.config/systemd/user/smart-panel.service`, `Restart=on-failure`) on Linux and a launchd agent plist on macOS, `install|uninstall|status`, create the runtime directory as the service account at install and on every service start (systemd `ExecStartPre`, launchd equivalent), print the one-time `loginctl enable-linger` hint on headless Linux. Test generated unit and plist contents in `tests/unit/test_service.py`.
- [ ] T061 [US1] Register built-in plugins in `smart_panel/plugins/__init__.py` (a registry listing `text` now; later phases add the others) and make the loader read it first. Until every built-in exists, the default config contains only registered built-ins and a default layout filtered to regions whose instance exists, so the default config always validates (depends on T025, T032, T043).
- [ ] T062 [US1] Run quickstart Scenarios A and C (generic) and the access policy checks in Scenario H, fix defects found, and confirm T036 to T042 pass (depends on T036 to T061 and T130).

- [ ] T130 [US1] Hardware smoke test of the MVP (constitution IV): run the runner with `HidSink` on the real panel for at least 10 minutes with a `text` instance, change config live, unplug and replug the panel, and send `brightness set` 0/50/100. Record hardware, OS, duration and results in the PR description and `handoff/panel_protocol_handoff.md` (depends on T051, T129).

**Checkpoint**: User Story 1 is fully functional and testable independently. This is the MVP: a managed, always-on runner with a CLI, usable with the `text` plugin.

---

## Phase 4: User Story 2 - Show alerts prominently, including ones pushed by an agent (Priority: P2)

**Goal**: Alerts pushed through the CLI (agent or any local process) appear over the main region within 2 s, always time out (the panel has no input), are tracked in a bounded history, and leave a missed-alert count for the sidebar.

**Independent Test**: Quickstart Scenarios B and C. A critical alert pushed through the CLI covers the main region while sidebar and crawl stay visible, clears itself after 60 s (fake clock), is listed as `timed_out` in history, and `alerts.missed` reports count 1 until `alert clear-missed`.

### Tests for User Story 2 (write first, confirm they fail)

- [ ] T063 [P] [US2] Unit tests for queue rules in `tests/unit/test_alert_queue.py`: order by severity (critical, warning, info) then arrival; a strictly higher severity preempts the displayed alert, which returns to the front of its severity band, is not counted as missed, and gets its full display time again; "queue cap 50" overflow drops the oldest `info`, then `warning`, recorded `expired_in_queue`; `max_wait_s` default 120 moves a waiting alert to `expired_in_queue`; default durations `info` 10, `warning` 20, `critical` 60 seconds, capped at `max_duration_s` default 300; the countdown starts at `shown_at`, not `received_at`; re-pushing an existing id updates it in place; a wall-clock jump or suspend/resume (fake clock) neither extends nor cuts short a displayed alert, because timers use the monotonic clock.
- [ ] T064 [P] [US2] Unit tests for history and missed alerts in `tests/unit/test_alert_history.py`: retention windows `1h` (default), `2h`, `4h`, and `today` (since local midnight) measured from `received_at`; pruning at least every 60 s; missed alerts survive pruning until cleared; a `resolve` removes a matching missed alert; `dismissed` and `resolved` are never counted as missed; `alerts.missed` publishes `{count, max_severity}`; a wall-clock jump does not corrupt retention pruning.
- [ ] T065 [P] [US2] Contract tests for alert verbs in `tests/contract/test_alert_commands.py`: `push|dismiss|resolve|list|show|clear-missed` argument and result shapes from `contracts/builtin-plugins.md`; severity other than `info`, `warning`, `critical`, empty message, or `ttl` below 1 exits 2; a message over 280 chars is truncated with a warning in the result; `push`, `dismiss`, `resolve`, `clear-missed` are `spoolable`; `assert_conforms` passes for the plugin.
- [ ] T066 [P] [US2] End-to-end test in `tests/integration/test_alerts_e2e.py` (file sink, fake clock): alert covers exactly the `main` region (pixel checks that sidebar and crawl areas are unchanged), a `+N queued` badge appears with several alerts, time-out records history and increments the missed count, `clear-missed` returns it to 0, a spooled alert older than its duration is recorded `expired_in_queue` and counted missed on runner start, and a flood of 500 alerts in a minute keeps frame time within budget, collapses low-severity alerts into the queued count, and logs every discarded alert.

### Implementation for User Story 2

- [ ] T067 [P] [US2] Implement `smart_panel/plugins/alerts/model.py`: `Alert` with `severity` one of `info`, `warning`, `critical` (anything else rejected), `title` "<= 80 chars, plain text", `message` "1..280 chars, plain text, control characters stripped", `source` "<= 40 chars", `duration_s` defaulted by severity and "capped at `max_duration_s` (default 300)", `received_at`, `shown_at`, `ended_at`, `state` in queued, displayed, dismissed, resolved, timed_out, expired_in_queue, and `outcome`; caller `id` or generated.
- [ ] T068 [US2] Implement `smart_panel/plugins/alerts/store.py` using `ctx.db()`: tables `alerts(id PK, ext_id, severity, title, message, source, duration_s, received_at, shown_at, ended_at, state, outcome)` and `missed_alerts(id PK, ext_id, severity, title, message, received_at, outcome)` with a schema version in its own `meta` table, retention pruning, and missed rows that survive pruning (depends on T067).
- [ ] T069 [US2] Implement `smart_panel/plugins/alerts/queue.py`: the state machine from `data-model.md` (queued to displayed to timed_out or dismissed or resolved; queued to expired_in_queue; preemption), one alert displayed at a time, max wait and queue cap handling, startup rule that alerts that were displayed when the runner stopped are treated as timed out (depends on T067).
- [ ] T070 [P] [US2] Implement `smart_panel/plugins/alerts/render.py`: overlay image sized to the target region, severity background colors (info `#2E7DD7`, warning `#E6A700`, critical `#D62839`), contrast-chosen text color, wrapped title (size 56) and message (size 40) never below `min_text_px`, a `+N queued` corner badge, `Inactive` when nothing is displayed.
- [ ] T071 [US2] Implement `smart_panel/plugins/alerts/__init__.py`: capability `overlay` plus verbs `push` (`--severity`, `--message`, `--title`, `--source`, `--id`, `--ttl`), `dismiss` (`--id` or `--current`), `resolve` (`--id`), `list` (`--state`, `--limit`, `--since`), `show`, `clear-missed` (`--id`); settings `durations`, `max_duration_s` 300, `max_wait_s` 120, `queue_cap` 50 ("5..200"), `history.retention` enum `1h|2h|4h|today` default `1h`, `colors`, `font`, `title_size`, `message_size`; publishes bus topic `alerts.missed` with no TTL; housekeeping at least every 60 s (depends on T068, T069, T070).
- [ ] T072 [US2] Register `alerts` in `smart_panel/plugins/__init__.py`, add its default instance and the `main` overlay to the default config, and add `alerts` verbs to the CLI schema output (depends on T061, T071).
- [ ] T131 [P] [US2] Contract test in `tests/contract/test_alert_source.py`: a sample plugin declaring `alert_source` calls `ctx.alert(...)` and the alert appears in the alerts instance with the same validation as `alert push`; a plugin without the capability gets an error; with no `alert_sink` instance configured the call fails with a clear message; `ctx.alert` is available in the `sdk.testing` harness.
- [ ] T132 [US2] Implement generic `ctx.alert` routing in `smart_panel/core/plugins.py` and `smart_panel/core/workers.py` (dispatch to the instance whose manifest declares `alert_sink`, no alerts-specific logic in the core), give the `alerts` plugin the `alert_sink` capability in T071, and extend `smart_panel/sdk/testing.py` accordingly (depends on T018, T019, T071).
- [ ] T073 [US2] Run quickstart Scenarios B and C, fix defects, and confirm T063 to T066 and T131 pass (depends on T063 to T072 and T132).

**Checkpoint**: User Stories 1 and 2 both work independently; an agent can push alerts and see them on the display.

---

## Phase 5: User Story 3 - News-style crawl (Priority: P3)

**Goal**: A seamless horizontal ticker scrolls items from the CLI, RSS/Atom feeds (Google News), and JSON endpoints (ESPN, per sport), with configurable colors, speed, font, and size, and last-known-items fallback.

**Independent Test**: Quickstart Scenario D. Five CLI items scroll in a seamless loop in red-on-black at the configured speed; changing speed or colors through the CLI applies without restart; with recorded Google News and ESPN responses the crawl shows both sources; an unreachable source keeps the last items and shows its error in `status`.

### Tests for User Story 3 (write first, confirm they fail)

- [ ] T074 [P] [US3] Record trimmed fixtures `tests/fixtures/google_news_sample.xml` (RSS 2.0, 5 items) and `tests/fixtures/espn_nfl_scoreboard.json` (3 events) from the live responses documented in `research.md` decision 11, plus `tests/fixtures/atom_sample.xml`.
- [ ] T075 [P] [US3] Unit tests for RSS/Atom parsing in `tests/unit/test_source_rss.py`: RSS 2.0 and Atom items, `max_items` default 30, entity-expansion and DTD attacks rejected through `defusedxml`, 2 MB response cap, only `http` and `https` URLs accepted, `interval_s` floor 30 default 300.
- [ ] T076 [P] [US3] Unit tests for JSON mapping in `tests/unit/test_source_json.py`: `list_path` `events`, templates with `{a.b.0.c}` placeholders, the ESPN preset yields text like `TB 0 @ DAL 0 - 10/8 - 8:15 PM EDT`, missing fields render as empty text without raising, `interval_s` floor 30, `timeout_s` at most 10.
- [ ] T077 [P] [US3] Unit tests for crawl rendering in `tests/unit/test_crawl_render.py` (fake clock): position is derived from the clock so a dropped frame causes no jump, the loop is seamless, `speed_px_s` in "20..600 (default 140)", direction left and right, items swap only at the loop seam, a long item is not cut mid-character, text truncated at 280 chars at a word boundary with an ellipsis, `round_robin` versus `grouped` ordering, placeholder when there were never any items, size never below `min_text_px`.
- [ ] T078 [P] [US3] Contract tests for crawl verbs in `tests/contract/test_crawl_commands.py`: `items set|add|clear`, `source add|set|remove|list`, `preset list` shapes; `items *` are `spoolable`; `assert_conforms` passes; invalid `speed_px_s` exits 2.
- [ ] T079 [P] [US3] Integration test in `tests/integration/test_crawl_sources.py` using a local HTTP server serving the fixtures: sources refresh on their interval without interrupting the scroll, a failing source keeps its last items and shows `last_error` in `status`, and the `status` JSON lists each source's `last_fetch`.

### Implementation for User Story 3

- [ ] T080 [P] [US3] Implement `smart_panel/core/http.py` (the runner side of `ctx.http`): bounded GET with a fixed User-Agent, 10 s timeout, 2 MB response cap, `http` and `https` only, limited redirects, `${secret:NAME}` header substitution, and secret redaction in errors.
- [ ] T081 [US3] Implement the `sources` plugin base in `smart_panel/plugins/sources/__init__.py`: capability `datasource`; `pushed` source type (bus topics `push.<name>` with `ttl_s` default 300, persisted through `ctx.kv`); verbs `data push TOPIC VALUE [--ttl S]` (`spoolable`), `data get`, `data list`, `source add|set|remove|list`; runs fetches through the scheduler with timeouts (depends on T035, T080).
- [ ] T082 [P] [US3] Implement `smart_panel/plugins/sources/rss.py`: RSS 2.0 and Atom parsing with `defusedxml.ElementTree`, publishes `<name>.items` (list of titles) (depends on T080).
- [ ] T083 [P] [US3] Implement `smart_panel/plugins/sources/http_json.py` and `smart_panel/plugins/sources/templating.py`: `url`, `list_path`, `fields` (name to template), `headers` (values may be `${secret:NAME}`), `timeout_s` at most 10, `interval_s` at least 30; the `{path.to.field}` template evaluator supports numeric array indices; publishes `<name>.<field>` and `<name>.items` (depends on T080).
- [ ] T084 [P] [US3] Create `smart_panel/plugins/crawl/presets.json` and `smart_panel/plugins/crawl/presets.py`: `espn-nfl`, `espn-nba`, `espn-mlb`, `espn-nhl`, `espn-epl`, `espn-ncaaf` (URL `https://site.api.espn.com/apis/site/v2/sports/<sport>/<league>/scoreboard`, `list_path: events`, a template over `competitions.0.competitors.*.team.abbreviation`, `.score` and `status.type.shortDetail`, default interval 60 s) and `google-news-search` / `google-news-topic` helpers building the RSS URL from `--query` or `--topic` (default interval 300 s); presets are configuration only.
- [ ] T085 [US3] Implement `smart_panel/plugins/crawl/model.py`: settings `fg` `#FFFFFF`, `bg` `#B00020`, `font`, `size` 44, `speed_px_s` "20..600 (default 140)", `direction` `left|right`, `separator` "<= 8 chars" default `"  •  "`, `order` `round_robin|grouped`, `show_source_labels` false, `placeholder` `"No headlines"`, `sources` list of `{id, kind: cli|rss|json, ...}`; `CrawlItem` text plain, control characters stripped, "<= 280 chars".
- [ ] T086 [US3] Implement `smart_panel/plugins/crawl/render.py`: pre-render the text strip once per item set (via `ctx` fonts), crop a moving window using `frame.now`, swap the item set only at the loop seam, and never draw text below `min_text_px` (depends on T085).
- [ ] T087 [US3] Implement `smart_panel/plugins/crawl/__init__.py`: capability `region` with `animated=True`; binds `cli`, `rss`, and `json` sources (CLI items held via `ctx.kv` and bus topics `crawl.items.<id>`), schedules refreshes with `ctx.spawn`, keeps last known items on failure, `describe_state` reports each source's `last_fetch` and `last_error`; verbs `items set|add|clear`, `source add|set|remove|list`, `preset list` (depends on T081 to T086).
- [ ] T088 [US3] Register `crawl` and `sources` in `smart_panel/plugins/__init__.py`, add default instances and the `crawl` region (depends on T072, T087).
- [ ] T089 [US3] Run quickstart Scenario D, fix defects, confirm T074 to T079 pass, and record a crawl frame-time measurement with the file sink (depends on T074 to T088).

**Checkpoint**: User Stories 1 to 3 all work independently.

---

## Phase 6: User Story 4 - Gauge and graph display (Priority: P4)

**Goal**: One to six gauges, each with a data source, aggregation rule and window, units, range, style, and color thresholds, with an explicit stale state, fed by system metrics, pushed values, and JSON endpoints.

**Independent Test**: Quickstart Scenario E. Four system-metric gauges appear in a four-up layout and update at their rates; crossing a threshold changes a gauge's color; adding a fifth and sixth adapts the grid; a seventh is rejected naming the maximum of 6; a stopped source shows the stale state.

### Tests for User Story 4 (write first, confirm they fail)

- [ ] T090 [P] [US4] Unit tests for aggregation in `tests/unit/test_aggregation.py`: `last`, `avg`, `min`, `max`, `sum` over a window of "10..86400" seconds, `last` ignores the window, the sample buffer is bounded at 3,600 points by averaging older samples into buckets, results are correct across bucket boundaries and across a wall-clock jump or suspend/resume (fake clock; windows use the monotonic clock).
- [ ] T091 [P] [US4] Unit tests for the `system` source in `tests/unit/test_source_system.py` with a fake `psutil`: metrics `cpu` and `mem` (percent), `temp` (degrees C via `sensors_temperatures()` with fallback to `/sys/class/thermal/thermal_zone0/temp`, `no_data` when unavailable), `disk:<path>` (percent), `net_rx` and `net_tx` (bytes per second computed from counter deltas), `interval_s` at least 1 with default 2.
- [ ] T092 [P] [US4] Unit tests for gauge rendering in `tests/unit/test_gauge_render.py`: grids for 1 to 6 gauges (1x1 up to 3x2), styles `dial`, `bar`, `numeric`, `sparkline`, thresholds change the color at the right value and recover, a stale or `no_data` topic dims the value and shows `no data`, text never below `min_text_px` (default `value_size` 72 and `label_size` 28, and configured sizes below the floor are clamped up), cached layers are reused until the value changes.
- [ ] T093 [P] [US4] Contract tests for gauge verbs in `tests/contract/test_gauge_commands.py`: `add|set|remove|list|push` shapes; a seventh gauge exits 2 with a message naming the maximum of 6; `gauge push` is `spoolable`; invalid `range` (min not below max), thresholds out of order or outside the range, `window_s` outside 10..86400 each exit 2; `assert_conforms` passes.
- [ ] T094 [P] [US4] End-to-end test in `tests/integration/test_gauges_e2e.py` (file sink, fake clock, fake psutil): Scenario E including the stale state and the seventh-gauge rejection.

### Implementation for User Story 4

- [ ] T095 [US4] Implement `smart_panel/plugins/sources/system.py` (source type `system`, uses `psutil`; settings `metrics` list of `cpu|mem|temp|disk:<path>|net_rx|net_tx` and `interval_s`), publishing `<name>.cpu`, `.mem`, `.temp`, `.disk:<path>`, `.net_rx`, `.net_tx`; register it in the `sources` plugin (depends on T081).
- [ ] T096 [P] [US4] Implement `smart_panel/plugins/gauges/aggregation.py`: bounded ring buffer of `(timestamp, value)` (in memory only) with the five aggregations over a configurable window.
- [ ] T097 [US4] Implement `smart_panel/plugins/gauges/model.py`: `Gauge` with `id` unique within the instance, `label` "<= 24 chars", `topic`, `aggregation` one of `last|avg|min|max|sum`, `window_s` "10..86400", `unit` "<= 6 chars", `range` "`[min, max]`, `min < max`", `style` one of `dial|bar|numeric|sparkline`, `thresholds` "list of `{at, color}`, ascending `at`, within range", `stale_after_s` default 3x the source interval (the refresh interval belongs to the data source, spec FR-032); the instance holds "between 1 and 6 gauges" and rejects more with a message naming the maximum (depends on T096).
- [ ] T098 [P] [US4] Implement `smart_panel/plugins/gauges/render.py`: grid layout sized to the gauge count, `dial` and `bar` drawn at 3x and downsampled for antialiasing, `numeric`, `sparkline` (recent history), per-gauge layer cache invalidated when the value, thresholds, or stale state change, stale state dims the value and shows `no data`.
- [ ] T099 [US4] Implement `smart_panel/plugins/gauges/__init__.py`: capability `region`; settings `gauges`, `layout` (`auto` or `{cols, rows}`), `bg` `#101418`, `font`, `value_size`, `label_size`; subscribes to bus topics and calls `ctx.request_render()` on change; verbs `add`, `set`, `remove`, `list`, `push ID VALUE` (publishes `push.<id>`, `spoolable`) (depends on T097, T098).
- [ ] T100 [US4] Register `gauges` in `smart_panel/plugins/__init__.py` and add default config: a `sys` system source and four default gauges (CPU, MEM, TEMP, DISK) in the `gauges` instance placed in `main` (depends on T088, T095, T099).
- [ ] T101 [US4] Run quickstart Scenario E, fix defects, and confirm T090 to T094 pass (depends on T090 to T100).

**Checkpoint**: User Stories 1 to 4 work independently.

---

## Phase 7: User Story 5 - Sidebar with app icons and count chips (Priority: P5)

**Goal**: A sidebar of generic app icons with count chips (counts supplied through the CLI or a generic data source), hide-when-zero, abbreviation, stale state, and a built-in alert entry showing the missed-alert count.

**Independent Test**: Quickstart Scenario F. Three entries show icons and chips; `sidebar count set mail 1204` shows `999+`; setting it to 0 hides the chip; the built-in alert entry shows the missed-alert count in the highest-severity color and hides at zero.

### Tests for User Story 5 (write first, confirm they fail)

- [ ] T102 [P] [US5] Unit tests for sidebar rendering in `tests/unit/test_sidebar_render.py`: entries at least 72 px tall with at most 6 including the alert entry (a 7th is rejected naming the maximum), `hide_when_zero` default true, `max_display` default 999 renders `999+`, a stale count renders `?`, label and chip text are never below `min_text_px` (defaults 24 and 24, clamped up if configured lower), a changed count gets a brief highlight, an unusable icon image falls back to the generic icon with a warning in `status`, `label` "<= 12 chars", the alert entry takes the color of the highest missed severity and is hidden at zero.
- [ ] T103 [P] [US5] Contract tests for sidebar verbs in `tests/contract/test_sidebar_commands.py`: `entry add|set|remove|list` and `count set ID N` shapes, `count set` is `spoolable`, unknown icon names and non-integer counts exit 2, `assert_conforms` passes.
- [ ] T104 [P] [US5] End-to-end test in `tests/integration/test_sidebar_e2e.py` (file sink, fake clock): Scenario F plus a cross-plugin check that an alert timing out increments the sidebar alert entry through the `alerts.missed` topic and `alert clear-missed` hides it, plus a pull-source case where an `http_json` (local test server) topic feeds a sidebar entry's count on its refresh interval (FR-039).

### Implementation for User Story 5

- [ ] T105 [P] [US5] Add bundled generic icons `mail`, `calendar`, `chat`, `alert`, `check`, `cloud`, `server`, `git`, `bell`, `home`, `shield`, `clock` as 64 px PNGs derived from Tabler Icons (MIT) in `smart_panel/assets/icons/` (no brand logos), add the Tabler Icons (MIT) entry to `THIRD_PARTY_NOTICES.md`, implement `smart_panel/core/icons.py` (lookup by name or PNG path with a generic fallback; an unusable icon file raises a warning that `status` lists).
- [ ] T106 [US5] Implement `smart_panel/plugins/sidebar/model.py`: entry fields `id` unique, `icon` bundled name or PNG path, `label` "<= 12 chars", `topic` carrying an integer count, `hide_when_zero` default true, `max_display` default 999, `stale_after_s` or null; the "<= 6 entries including the built-in alert entry" limit.
- [ ] T107 [P] [US5] Implement `smart_panel/plugins/sidebar/render.py`: icon with label, count chip (`chip_fg` `#FFFFFF`, `chip_bg` `#D62839`), `999+` abbreviation, `?` stale chip, brief change highlight, alert entry colored by `max_severity` (depends on T106).
- [ ] T108 [US5] Implement `smart_panel/plugins/sidebar/__init__.py`: capability `region`; settings `entries`, `bg` `#0B0E11`, `chip_fg`, `chip_bg`, `font`, `label_size`, `chip_size`; verbs `entry add|set|remove|list` and `count set ID N` (sets a bus topic with a TTL, `spoolable`); implicit alert entry bound to `alerts.missed` using only the bus (depends on T105 to T107).
- [ ] T109 [US5] Register `sidebar` in `smart_panel/plugins/__init__.py`, add its default instance (empty entry list plus the alert entry), and complete the default layout so all four built-ins are present; run quickstart Scenario F and confirm T102 to T104 pass (depends on T100, T108).

**Checkpoint**: All four requested features work independently and together on the default layout.

---

## Phase 8: User Story 6 - Extend the display with new plugins (Priority: P6)

**Goal**: A developer can add a new plugin without touching core, test it without a panel, and a misbehaving plugin cannot take down other regions; the built-ins prove the contract.

**Independent Test**: Quickstart Scenario G. A sample drop-in plugin is listed and renderable with no core changes; a deliberately crashing or stalling plugin leaves all other regions working and is reported `degraded` or `stalled` in `status`.

### Tests for User Story 6 (write first, confirm they fail)

- [ ] T110 [P] [US6] Parametrized conformance test in `tests/contract/test_builtin_conformance.py` running `assert_conforms` on every built-in plugin (`text`, `sources`, `alerts`, `crawl`, `gauges`, `sidebar`).
- [ ] T111 [P] [US6] Isolation test in `tests/integration/test_plugin_isolation.py`: test plugins that raise in `render`, return the wrong size, return the wrong type, and sleep past the 2 s hard deadline; every other region keeps rendering, `status` shows `degraded` or `stalled` with the last error, an error badge appears in the failing region, and the runner survives and can remove the plugin.
- [ ] T112 [P] [US6] Drop-in test in `tests/integration/test_dropin_plugin.py`: copy a sample plugin file into `<home>/plugins/`, `plugin list` shows it, it is assigned to a region by configuration alone and renders, invalid settings are rejected from its declared settings, removing it leaves the runner unaffected; also a name clash with a built-in, a bad name, and a broken import are reported as `load_failed`.

### Implementation for User Story 6

- [ ] T113 [P] [US6] Write the sample third-party plugin `examples/plugins/clock.py` (region plugin using only `smart_panel.sdk`, with a `format` setting, an `animated` flag, and a `crash` verb used by tests and the quickstart).
- [ ] T114 [P] [US6] Write `docs/plugin-authoring.md`: manifest, settings, lifecycle, render rules (use `frame.now`, never block, deadlines), commands and `spoolable`, data source plugins, alert-source plugins (`alert_source` capability and `ctx.alert`), the `sdk.testing` harness with a worked example, entry-point packaging snippet for `pyproject.toml`, and the isolation limits from `contracts/plugin-sdk.md`.
- [ ] T115 [US6] Complete plugin management in the CLI and core: `plugin list` shows `load_failed` and `incompatible` states, `plugin describe` prints settings with types, ranges, and defaults, setting rejections quote the plugin's declared constraints, and `schema` includes third-party plugin verbs (depends on T058, T059, T113).
- [ ] T116 [US6] Run quickstart Scenario G, fix defects, and confirm T110 to T112 pass (depends on T110 to T115).

**Checkpoint**: All six user stories are independently functional.

---

## Phase 9: Polish and Cross-Cutting Concerns

**Purpose**: Cross-story verification, documentation, and the Raspberry Pi hardware validation required by SC-001 to SC-009.

- [ ] T117 [P] Add `tests/contract/test_schema_coverage.py`: every command and plugin verb listed in `quickstart.md` appears in `smart-panel schema` output with arguments, and every config key in the data model is reachable through `config set` (SC-008).
- [ ] T118 [P] Add `tests/integration/test_security.py`: secrets never appear in `status`, `logs`, `config show`, or error messages; `secrets.json` is mode 0600; hostile text (control characters, markup, 100 KB strings) in alerts, crawl items, and counts is rendered as bounded plain text; spool files from non-allowed owners are rejected; a non-allowed peer receives exit 5.
- [ ] T119 [P] Write `scripts/profile_frame.py`: compose and encode a frame with all four built-ins active (6 gauges, crawl with 30 items, sidebar with 5 entries, alert shown) and report median and p95 time and JPEG size; run it on the dev machine and record results in `research.md`.
- [ ] T120 [P] Update `README.md` with the CLI quick tour, default layout screenshot (from `preview`), plugin authoring link, and the security notes (local-only control channel, spool in the temp directory, in-process plugin isolation limits).
- [ ] T121 [P] Update `CLAUDE.md` Architecture and Commands sections to describe the implemented `smart_panel` layers, run commands, and test layout (preserve the Orchestration section and the Spec Kit block); refresh the agent-context block with `uv run --no-project --with pyyaml bash .specify/extensions/agent-context/scripts/bash/update-agent-context.sh`.
- [ ] T122 Review `.specify/memory/constitution.md` against the delivered implementation (Principles I to V and the Contribution Workflow, including that CI and pytest now exist) and amend it only if practice diverged, bumping the version per its Governance section; the earlier CI and test TODOs were already removed with the Sync Impact Report comment in commit `0330837`.
- [ ] T123 Raspberry Pi 4B hardware P1: install on the Pi (`packaging/99-smart-panel.rules`, `pip install -e .`), run `scripts/bench_throughput.py`, and record packets per second and sustainable fps in `handoff/panel_protocol_handoff.md` and `research.md` decision 1.
- [ ] T124 Raspberry Pi P2 (SC-001, SC-003): start with the default layout plus Scenario D, E, and F data; verify content within 15 s of `start`, crawl at 10 fps or better, alert within 2 s (push 100 alerts one at a time and report the share shown within 2 s, target at least 95%, SC-002); run `scripts/profile_frame.py` on the Pi and record median and p95 compose plus encode time (target <= 40 ms p95); save `preview` frames for the README.
- [ ] T125 Raspberry Pi P3 (SC-007): unplug and replug the panel, and separately power-cycle it, while running; content must return within 10 s of availability; write the recovery runbook `docs/runbook.md` (hung controller, udev, linger, logs).
- [ ] T126 Write `scripts/soak.py` (logs runner RSS, CPU, measured fps, and error count every minute to CSV and starts a CPU/memory load stand-in for the agent), then run a 72 h soak on the Pi (SC-005, SC-006): CPU at most 50% of one core on average, RSS under 500 MB and within 10% of its hour-one value at hour 72, no crash.
- [ ] T127 Install the service on the Pi with `smart-panel service install`, enable linger, reboot, and confirm the display returns unattended; record hardware, OS, Python version, and durations in the pull request (constitution Principle IV).
- [ ] T128 Final verification: run `ruff check .` and `pytest` on Linux and macOS CI, run quickstart Scenarios A to H, confirm every spec success criterion SC-001 to SC-010 has a recorded result, and complete the "Done checklist" at the end of `quickstart.md`.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies. Must be green in CI before any feature work merges.
- **Foundational (Phase 2)**: Depends on Phase 1. Blocks all user stories.
- **User Story 1 (Phase 3, P1)**: Depends on Phase 2. This is the MVP.
- **User Story 2 (Phase 4, P2)**: Depends on Phase 3 (runner, CLI, control channel, plugin registry).
- **User Story 3 (Phase 5, P3)**: Depends on Phase 3; needs no alerts code. Shares `sources` with Phase 6.
- **User Story 4 (Phase 6, P4)**: Depends on Phase 3 and on the `sources` plugin base from Phase 5 (T081); can start earlier if T081 is pulled forward.
- **User Story 5 (Phase 7, P5)**: Depends on Phase 3; the built-in alert entry reads the `alerts.missed` topic, so full end-to-end verification (T104) needs Phase 4.
- **User Story 6 (Phase 8, P6)**: Depends on Phase 3; T110 needs all built-ins registered (Phases 4 to 7).
- **Hardware smoke tasks**: T129 (needs T007) gates the Phase 1 checkpoint T015; T130 (needs T051 and T129) gates T062; T132 (needs T018, T019, T071) gates T073 together with T131.
- **Polish (Phase 9)**: Depends on the stories in scope. Hardware tasks T123 to T127 need a working install on the Pi (Phases 3 to 7).

### Within Each User Story

- Tests first, and they must fail before implementation.
- Models before services, services before plugin wiring, wiring before registration and the quickstart run.
- Each story ends with its quickstart scenario and a checkpoint.

### Parallel Opportunities

- Phase 1: T003 to T006, T008 to T013 can run in parallel after T001/T002.
- Phase 2: T016 to T020, T022, T025 to T027, T031, T033 to T035 are in different files and mostly independent; T021, T023, T024, T028 to T030, T032 depend on earlier items as noted.
- After Phase 3, Stories 2, 3, and 5 can proceed in parallel; Story 4 follows T081.
- Within every story, all `[P]` tests can be written together, and `[P]` model or render modules in different files can be built together.

### Parallel Example: User Story 1 tests

```bash
Task: "Contract test for the CLI envelope in tests/contract/test_cli_envelope.py"          # T036
Task: "Unit test for the access policy in tests/unit/test_access.py"                        # T037
Task: "Contract test for the control protocol in tests/contract/test_control_protocol.py"   # T038
Task: "Integration test for panel loss in tests/integration/test_panel_loss.py"             # T042
```

### Parallel Example: User Story 3 implementation

```bash
Task: "Implement http helper in smart_panel/core/http.py"                          # T080
Task: "Implement RSS source in smart_panel/plugins/sources/rss.py"                 # T082 (after T080)
Task: "Implement JSON source in smart_panel/plugins/sources/http_json.py"          # T083 (after T080)
Task: "Create crawl presets in smart_panel/plugins/crawl/presets.json"             # T084
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 (Setup and CI), then Phase 2 (Foundational).
2. Phase 3 (US1). **Stop and validate** with quickstart Scenarios A, C, and H, ideally on the real panel on a Pi.
3. A managed, always-on runner with a CLI and a `text` plugin is a demonstrable first release.

### Incremental Delivery

1. Add US2 (alerts), the headline agent capability, and validate Scenarios B and C.
2. Add US3 (crawl and sources), then US4 (gauges), then US5 (sidebar), validating each scenario and the default layout after each.
3. Add US6 (plugin authoring polish), then Phase 9 hardware validation on the Pi.

### Suggested pull request grouping

One pull request per phase (Phase 1 first), each with its tests, and hardware verification notes on any PR that touches the sink or runner (constitution Principle IV).

---

## Notes

- `[P]` means different files and no dependency on an incomplete task.
- Task counts: Setup 17, Foundational 20, US1 28, US2 13, US3 16, US4 12, US5 8, US6 7, Polish 12 (133 total). T129 to T133 were added after analysis and sit in their phases out of numeric order.
- Verify tests fail before implementing; commit after each task or logical group (auto-commit is off in `.specify/extensions/git/git-config.yml`).
- Avoid: vague tasks, two tasks editing the same file in parallel, and cross-story dependencies beyond those listed above.
