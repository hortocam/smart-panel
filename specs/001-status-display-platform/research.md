# Research: Status Display Platform

All Technical Context unknowns are resolved below. Each entry is **Decision / Rationale / Alternatives considered**. Measurements marked *(measured)* come from `handoff/fullpaneltest.pcapng` or live requests made on 2026-10-07.

## 1. Panel throughput and frame rate

**Decision**: Treat the USB link as capable of at least 15 fps for typical frames, default the target to 15 fps, and add a Phase 0 benchmark script to measure the real figure on the Pi.

**Rationale** *(measured)*: In the vendor capture, 340 `CRTDRA` frames were sent in 19.3 s (17.6 fps average, median interval 61 ms). Each 58-packet frame (about 58 KB) took a median of 13 ms to transfer, roughly 0.23 ms per packet, so the vendor is pacing itself rather than being bandwidth-limited. The handoff doc's older note of "12 ms per packet" does not match this capture and should be corrected after the Pi benchmark. The capture was taken on Windows; hidapi on Linux (hidraw) and macOS may be slower, which is why the benchmark is a Phase 0 deliverable and the governor (decision 9) exists.

**Alternatives considered**: Hard-coding a lower fps (rejected: SC-003 needs 10 fps or better and the data says it is achievable); trusting the capture without a benchmark (rejected: different host stack).

## 2. Frame size limit and encoding

**Decision**: Rotate, then JPEG-encode with a quality ladder (70, 60, 50, 40, 30), starting from the last quality that fit. If the frame is still over 65,503 bytes of JPEG (65,535 minus the 32-byte preamble) at the lowest rung, keep showing the last good frame and report `frame_too_large` in status. Raise quality again after a streak of small frames.

**Rationale**: The length field is a big-endian uint16, so a hard limit of 65,535 bytes exists (already enforced in `protocol.py`). Gauge and crawl content is flat-color and compresses well; photographs are the risk. FR-010 requires automatic adjustment.

**Alternatives considered**: Downscaling the canvas (rejected: blurs text); chroma subsampling changes only (kept as a fallback rung); rejecting the frame outright (rejected by FR-010).

## 3. Concurrency model

**Decision**: Threads. A compositor loop at the target fps; one render worker per plugin instance; a sender thread with a single-slot "latest frame wins" buffer (so slow USB writes never queue stale frames); a control-server thread; a small thread pool (max 4) for data-source fetches with timeouts; a housekeeping timer (retention, spool ingest).

**Rationale**: Pillow and JPEG encoding release the GIL for the heavy parts; HID writes and sockets are I/O. Threads give isolation without asyncio colouring across third-party plugin code. Crawl position is computed from the clock, not frame count, so dropped frames never cause visible jitter.

**Alternatives considered**: asyncio (rejected: third-party plugins and `hid` are blocking; mixing would need executors anyway); multiprocessing (rejected: memory on a 4 GB Pi shared with an agent); a single synchronous loop (rejected: one slow plugin would stall the panel, violating FR-045).

## 4. Plugin discovery, SDK, and isolation

**Decision**: Plugins are Python classes with a declarative `Manifest`. Discovery: (a) the `smart_panel.plugins` entry-point group for installed packages, and (b) `*.py` files or packages in `<home>/plugins/` for drop-in use (US6 independent test). Capabilities: `region` (renders into a region), `overlay` (renders above a region, returns nothing when inactive), `datasource` (declares source types), and `commands` (CLI verbs). Each render runs in the plugin's own worker with a deadline (default 250 ms soft, 2 s hard); on overrun or exception the last good image is kept, the plugin is marked `degraded` or `stalled`, and an error tile may be drawn. Built-in plugins import only from `smart_panel.sdk`, enforced by a contract test (FR-043).

**Rationale**: Meets FR-040 to FR-047 with the least machinery. Entry points are the standard Python mechanism, and the drop-in directory serves single-file experiments.

**Known limit**: A thread cannot be killed. A plugin stuck in a busy loop can still consume CPU or the GIL, and a stalled worker is abandoned, not terminated; the runner reports it and a restart clears it. Out-of-process plugins are a future option if third-party plugins become common. This limit is documented in the SDK contract.

**Alternatives considered**: Subprocess per plugin (rejected for RAM); `exec`-style config-only widgets (rejected: cannot add new data sources); no isolation (rejected by FR-045).

## 5. Control channel and CLI/runner split

**Decision**: The runner serves JSON-lines over a Unix domain socket (`<runtime>/control.sock`). The CLI is a thin client. Commands are namespaced (`runner.*`, `config.*`, `plugin.*`, `plugin.call`, `access.*`, `logs.*`). Plugin verbs (for example `alert push`) are declared in manifests, so the CLI builds its subcommand tree from manifests (cached in `<home>/cli-cache.json`, rebuilt when the runner reports a plugin-set change or on `--refresh`), and the same verbs are reachable generically via `plugin call <instance> <verb>`.

**Rationale**: Satisfies FR-004, FR-005, FR-007 and FR-043 (no feature logic in the core). A request/response socket returns validation errors and results, unlike signals.

**Alternatives considered**: HTTP on localhost (rejected: opens a network surface that the spec deferred with the webhook; Unix sockets get kernel-level peer credentials); file-based commands only (rejected: no feedback); D-Bus (rejected: not on macOS).

## 6. Access control for the CLI

**Decision**: The socket lives at `<tmpdir>/smart-panel-<uid>/control.sock` (directory 0755, socket 0666). Every connection is authenticated by kernel peer credentials: `SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS (isolated in `control/peercred.py`). The pure function `access.is_allowed(peer_uid, peer_groups, policy)` allows the runner's own uid, root, any uid in `access.allowed_users`, and any member of `access.allowed_group`. Everyone else gets exit code 5 and the event is logged. `access.*` commands are accepted only from the runner's uid or root.

**Rationale**: FR-051 (clarified: own account plus owner-configured accounts or group). Doing the check in the runner means one policy applies to every command, whether or not file permissions would have allowed access. The home directory stays 0700, which is why the socket is not placed there.

**Alternatives considered**: Filesystem permissions only (rejected: cannot express an allow-list of individual accounts); a shared token (rejected: a secret to distribute, and no better than peer credentials locally).

## 7. Pushes while the runner is down (spool)

**Decision**: If the CLI cannot connect, push-type commands (alerts, resolve, dismiss, counts, values, crawl items) are written to `<tmpdir>/smart-panel-<uid>/spool/` as one JSON file each (mode 0644 inside a sticky 1733 directory) and the CLI reports `saved_for_later`. On startup the runner ingests the spool in receive-time order, verifies each file's owner uid against the access policy, and applies it. Alerts whose display duration has already elapsed since `received_at` are recorded as `expired_in_queue` and counted as missed (clarified behaviour).

**Rationale**: FR-005, FR-007, FR-018 and the edge case on replay. File ownership cannot be forged by an unprivileged user, so the access policy still holds.

**Known limits**: The spool is in the temp directory, so a reboot clears it; an agent that pushed seconds before a reboot loses that push. A disk-backed spool directory is configurable (`paths.spool`) for installs where that matters. On a multi-user machine another account could fill the spool with junk; the runner rejects non-allowed owners at ingest and caps the spool at 1,000 files.

**Alternatives considered**: CLI writing directly into the SQLite database (rejected: bypasses the access policy and needs a shared writable home); returning an error (rejected by the clarification).

## 8. Persistent state

**Decision**: SQLite (stdlib, WAL mode). Core `<home>/state.db` holds `topics` (pushed values and counts with timestamps) and `meta`; each plugin that needs tables gets a private database through the SDK (`ctx.db()`, `<home>/state/<instance>.db`), which is where the alerts plugin keeps `alerts` and `missed_alerts`. The JSON config at `<home>/config.json` is written atomically (temp file plus rename) after a change has been validated and applied. Secrets live in `<home>/secrets.json` (mode 0600) and are referenced from config as `${secret:NAME}`.

**Rationale**: Alert history, the missed-alert count, and pushed values must survive restarts (FR-007, FR-022, FR-024). Keeping `missed_alerts` separate from `alerts` lets history be pruned by retention while the missed count persists until cleared (spec edge case). Secrets stay out of config, logs, and status (FR-049).

**Alternatives considered**: One JSON file for everything (rejected: concurrent writers, pruning, crash safety); a full ORM (rejected: dependency weight).

## 9. Load shedding on a shared Pi

**Decision**: The runner calls `os.nice(5)` at start. A governor measures per-frame time and process CPU every 5 s; if frames overrun their budget for 3 consecutive windows, it steps fps down (15, 12, 10, 8, 5) and doubles source refresh intervals (capped at 4x); it steps back up after 60 s of headroom. Every change is logged and visible in `status`.

**Rationale**: FR-048 ("degrade rather than starve") and the shared-host edge case. Niceness keeps the agent responsive without any coordination.

**Alternatives considered**: cgroup limits (rejected: needs root and systemd specifics); fixed low fps (rejected: wastes the Pi when idle).

## 10. Configuration format and validation

**Decision**: JSON, versioned (`"version": 1`) with a migration hook. Settings are described by a small in-house `SettingSpec` (type, default, min/max, enum, pattern, description, secret flag) used for validation, for the CLI `config schema` output, and for plugin manifests. Dotted-path edits (`config set plugins.crawl.settings.speed 120`) validate the whole candidate config before applying.

**Rationale**: JSON is stdlib in both directions and what agents emit natively; the CLI is the intended editor (FR-004). An in-house spec avoids pulling a validation framework onto the Pi.

**Alternatives considered**: TOML (rejected: stdlib can read but not write); YAML (rejected: extra dependency and parsing hazards); pydantic (rejected: install weight and plugin authors would depend on it).

## 11. RSS/Atom and JSON sources

**Decision**: `urllib.request` with a 10 s timeout, a 2 MB response cap, `https` and `http` only, and a fixed User-Agent. RSS 2.0 and Atom are parsed with `defusedxml.ElementTree`. JSON endpoints use a small field-mapping language: a headline template with `{path.to.field}` placeholders evaluated over each element of a configurable list path (for example `events`). Conditional text and array indexing are supported (`{competitions.0.competitors.0.team.abbreviation}`).

*(measured, 2026-10-07)*: Google News search/topic RSS returns RSS 2.0 (`news.google.com/rss/search?q=...&hl=en-US&gl=US&ceid=US:en`, 102 items in a test query); its notice limits use to personal, non-commercial feed readers. ESPN scoreboards return JSON at `site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard` for football/nfl, basketball/nba, baseball/mlb, hockey/nhl, soccer/eng.1, and football/college-football (all HTTP 200; the NFL response was 270 KB with 15 events). Useful fields: `events[].shortName`, `events[].status.type.shortDetail`, `events[].status.type.state`, `events[].competitions[0].competitors[].team.abbreviation`, `.score`, `.homeAway`.

**Presets shipped** (configuration, not code): `espn-nfl`, `espn-nba`, `espn-mlb`, `espn-nhl`, `espn-epl`, `espn-ncaaf`, each producing text such as `TB 0 @ DAL 0 - 10/8 - 8:15 PM EDT`; and `google-news-search` / `google-news-topic` helpers that build the feed URL from a query.

**Risks**: The ESPN endpoints are not an official, documented API and may change; the crawl keeps the last known items, status shows the failure, and mappings are data so they can be fixed without a release (Spec assumption). Fetch intervals default to 5 minutes (news) and 60 seconds (scores) with a floor of 30 seconds to be a polite client.

**Alternatives considered**: `feedparser` (rejected: heavier, more surface); `httpx`/`requests` (rejected: extra dependency for simple GETs); JSONPath library (rejected: the placeholder syntax is enough and has no dependency).

## 12. System metrics

**Decision**: `psutil` for CPU percent, memory, disk usage, network throughput (computed rate), and temperature (`sensors_temperatures()`, falling back to `/sys/class/thermal/thermal_zone0/temp` on the Pi). Unsupported metrics (for example temperature on macOS) report `no_data`, which gauges show as stale.

**Rationale**: FR-033; wheels exist for Pi OS 64-bit and macOS.

**Alternatives considered**: Reading `/proc` and `/sys` directly (rejected: not portable to macOS, which is a first-class target).

## 13. Aggregation windows

**Decision**: Each gauge keeps an in-memory ring buffer of `(timestamp, value)` samples bounded by window length and a hard cap of 3,600 samples (older samples are bucketed by averaging when the window is long). Supported aggregations: last, avg, min, max, sum over a window of 10 s to 24 h. Samples are not persisted; after a restart the window refills.

**Rationale**: FR-034 with bounded memory. The spec does not require surviving restarts for gauge history.

**Alternatives considered**: Persisting samples in SQLite (deferred: adds write load on an SD card for no stated requirement).

## 14. Rendering approach

**Decision**: Pillow only. Each region has a cached RGBA layer re-rendered only when its plugin reports a change; the crawl pre-renders its text strip once per item set and crops a moving window per frame; gauge dials and chips are drawn at 3x and downsampled for antialiasing, cached until the value changes. Composition is a handful of pastes plus a transpose, then JPEG. Text uses a bundled TTF with a configured fallback chain; a missing glyph draws the font's replacement box and never raises.

**Rationale**: SC-003 and SC-005 on a Pi. Most frames only differ in the crawl, so most work is cached. Compose-plus-encode is the single cost per frame (estimated 15 to 30 ms on a Pi 4B for 0.9 MP; to be confirmed in Phase 0/1 benchmarks).

**Alternatives considered**: Cairo/Skia (rejected: heavy native dependencies, no compelling gain); OpenCV (rejected: size); drawing directly in portrait orientation (rejected for now: complicates every plugin; revisit if profiling shows the transpose matters).

## 15. Fonts and icons (legal)

**Decision**: Bundle DejaVu Sans (and Bold) for broad glyph coverage (its Bitstream Vera-derived license permits redistribution) and let users point to any other TTF by path. Bundle generic icons (mail, calendar, chat, alert, check, cloud, server, and a few more) as 64 px PNGs derived from a permissively licensed set (Tabler Icons, MIT). **No brand logos** (no Gmail or Google Calendar marks), to avoid trademark issues in an MIT repository; "mail" is a generic envelope. Attributions go in `THIRD_PARTY_NOTICES.md`. Emoji are not in the bundled font; unsupported glyphs show a fallback box (FR spec edge case) unless the user configures an emoji-capable font.

**Alternatives considered**: Inter or Roboto (rejected: smaller glyph coverage for symbols and non-Latin text); brand icons (rejected: licensing).

## 16. Service management

**Decision**: `smart-panel service install` writes a systemd **user** unit (`~/.config/systemd/user/smart-panel.service`, `Restart=on-failure`) on Linux and a launchd agent plist on macOS, then enables it. On a headless Pi it prints the one-time `loginctl enable-linger` hint. `start`, `stop` and `restart` delegate to the service manager when installed, and otherwise spawn or signal a detached runner process tracked by a pidfile plus an `flock` on a lock file (single-instance rule, FR-008).

**Rationale**: FR-013 without root. User units keep the runner in the same account the agent and CLI use, matching the default access policy.

**Alternatives considered**: System-wide unit (rejected: needs root and a dedicated user); cron `@reboot` (rejected: no restart on failure).

## 17. Panel connection handling

**Decision**: The `HidSink` owns open/close, retries with backoff (1, 2, 4, 8, 10 s cap), sends CRTDIS+CRTLIG on every successful open, reapplies brightness, and counts consecutive write failures. After 3 consecutive failures it closes the handle and re-enters the reconnect loop. If the device opens but writes keep failing for more than 60 s, status reports `panel_unresponsive` with a power-cycle hint (the controller hang observed without the init sequence cannot be recovered in software).

**Rationale**: FR-009, FR-010, Principle II, SC-007.

## 18. Alert semantics decided at plan level

These fill gaps the spec leaves to design; each is testable and recorded in `data-model.md`.

- **Ordering**: queue ordered by severity (critical, warning, info), then arrival.
- **Preemption**: a strictly higher-severity alert preempts the one on screen; the preempted alert returns to the front of its severity band, is not counted as missed, and receives its full display time when shown again.
- **Queue limits**: max wait 120 s (configurable); queue cap 50 (oldest `info` dropped first, recorded as `expired_in_queue` and counted as missed); max display duration 300 s.
- **Resolve**: a resolve for an identifier clears it wherever it is (on screen, queued, or in `missed_alerts`).
- **Identifiers**: if none is supplied, the runner assigns one; re-pushing the same identifier replaces the earlier alert (update-in-place), which is how monitoring systems re-send.

## 19. Sidebar capacity (plan-level choice)

**Decision**: Entries are 72 px tall minimum, so the default 192×462 sidebar fits 6 entries including the built-in alert entry; configuration with more is rejected with the maximum named.

**Rationale**: The spec sets no limit; a bounded layout keeps text legible (FR-016).

## 20. CI

**Decision**: GitHub Actions on `ubuntu-latest` and `macos-latest` with Python 3.11 and 3.13; steps: install with the `dev` extra, `ruff check`, `pytest -m "not hardware"`. Hardware tests run only locally. The `hid` package needs the hidapi shared library at import time on some systems, so CI installs it where required (`libhidapi-hidraw0` on Ubuntu, `hidapi` via Homebrew on macOS) and tests that import `panel_driver.device` do so lazily so pure-logic tests do not need it.

**Alternatives considered**: tox/nox (rejected: unnecessary indirection for one workflow); self-hosted Pi runner (deferred to the hardening phase).
