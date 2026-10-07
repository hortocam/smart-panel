# Data Model: Status Display Platform

Entities, fields, validation, and state machines derived from the spec. Types are descriptive; storage is covered at the end. Requirement ids refer to [spec.md](spec.md).

## Paths and locations

| Name | Default | Notes |
|---|---|---|
| `home` | `$SMART_PANEL_HOME` or `~/.config/smart-panel` (mode 0700) | `config.json`, `secrets.json` (0600), `state.db`, `state/<instance>.db`, `plugins/`, `runner.log*`, `cli-cache.json` |
| `runtime` | `$SMART_PANEL_RUNTIME`, else config `paths.runtime`, else `/tmp/smart-panel` (mode 0755, same path for every account, owned by the runner's uid) | `control.sock` (0666), `runner.lock`, `runner.pid`, `spool/` (sticky 1733) |
| `spool` | `<runtime>/spool` | Overridable with `paths.spool` for a disk-backed spool |

## Configuration

Single JSON document (`config.json`), validated as a whole before it is applied (FR-006).

```text
Config
├── version: int                       # 1
├── display
│   ├── fps: int                       # 5..30, default 15
│   ├── brightness: int                # 0..100, default 50
│   ├── jpeg_quality: {start: int 30..95 = 70, min: int 20..start = 30}
│   ├── min_text_px: int               # 12..64, default 24 (FR-016)
│   ├── sink: "hid" | "files" | "null" # default "hid"
│   └── files_dir: path | null         # used by the "files" sink
├── access
│   ├── allowed_users: [str]           # default []
│   └── allowed_group: str | null      # default null
├── paths: {spool: path | null}
├── layout: Layout
├── plugins: {name: PluginInstance}    # instance name -> instance
└── sources: {name: DataSource}
```

Validation: unknown keys are rejected with the dotted path of the offender; every instance's `settings` is validated against its plugin manifest; every region and overlay must reference an existing, enabled instance whose capabilities fit; gauge count and sidebar entry count limits apply (see plugins).

### Layout, Region, Overlay (FR-014, FR-015)

| Field | Type | Rules |
|---|---|---|
| `canvas` | fixed `1920x462` | Not configurable |
| `regions[]` | list of Region | Names unique; rectangles inside the canvas; regions may not overlap |
| `overlays[]` | list of Overlay | Each overlay targets one region by name |

**Region**: `name`, `x`, `y`, `w`, `h` (pixels, min 64 per side), `instance` (name of a plugin instance with the `region` capability).

**Overlay**: `region` (target region name), `instance` (plugin instance with the `overlay` capability), optional `z` (default 0). An overlay is drawn over its whole target region only while its plugin returns an image; otherwise it costs nothing.

**Default layout** (1920x462):

| Region | x | y | w | h | Instance |
|---|---|---|---|---|---|
| `sidebar` | 0 | 0 | 192 | 462 | `sidebar` |
| `main` | 192 | 0 | 1728 | 382 | `gauges` |
| `crawl` | 192 | 382 | 1728 | 80 | `crawl` |

Overlay: `main` ← `alerts`. The alert covers the gauge area; sidebar and crawl stay visible (clarified).

### PluginInstance

| Field | Type | Rules |
|---|---|---|
| `plugin` | str | Name of an available plugin |
| `enabled` | bool | Default true; a disabled instance cannot be referenced by the layout |
| `settings` | object | Validated against the plugin manifest's setting specs; secret settings use `${secret:NAME}` |

State (runtime only): `starting`, `ok`, `degraded` (errors but still rendering), `stalled` (render exceeded hard deadline), `failed` (start or validation error), `disabled`. Reported by `status` with the last error and time (FR-003, FR-045).

### DataSource (FR-033, FR-028)

| Field | Type | Rules |
|---|---|---|
| `type` | str | `system`, `pushed`, `http_json`, `rss` (contributed by the `sources` plugin; others via plugins) |
| `interval_s` | int | Floor 1 for `system`, 30 for network types |
| `settings` | object | Type-specific (see [builtin-plugins](contracts/builtin-plugins.md)) |

A source publishes samples to **topics** on the bus. Topic names are `<source>.<metric>` for sources and `push.<name>` for pushed values.

## Runtime entities

### Topic sample (bus)

| Field | Type | Notes |
|---|---|---|
| `topic` | str | Dotted name |
| `value` | number, string, list, or object | JSON-compatible |
| `ts` | float | Wall-clock seconds of the last update |
| `ttl_s` | int \| null | After this age the topic reports `stale` |

Freshness: `fresh` (age <= ttl), `stale` (age > ttl), `no_data` (never set). Consumers (gauges, sidebar, crawl) turn `stale` and `no_data` into their visible stale states (FR-036, FR-040).

### Gauge (settings of one gauge in the `gauges` plugin; 1..6, FR-031, FR-032)

| Field | Type | Rules |
|---|---|---|
| `id` | str | Unique within the plugin instance |
| `label` | str | <= 24 chars |
| `topic` | str | Bus topic or source metric to read |
| `aggregation` | enum | `last`, `avg`, `min`, `max`, `sum` |
| `window_s` | int | 10..86400; ignored for `last` |
| `unit` | str | <= 6 chars |
| `range` | `[min, max]` | `min < max` |
| `style` | enum | `dial`, `bar`, `numeric`, `sparkline` |
| `thresholds` | list of `{at, color}` | Ascending `at`, within range |
| `stale_after_s` | int | Default 3x the source interval |

Sample buffer: bounded ring (<= 3,600 points), in memory only.

### Crawl Source and Item (FR-026 to FR-030)

**Crawl Source**: `{id, kind, ...}` with `kind` in `cli` (items set by CLI), `rss` (`url`, `max_items`), `json` (`url`, `list_path`, `template`, `max_items`, optional `headers` using secrets). Each has its own `interval_s` (floor 30) and freshness.

**Crawl Item**: `{text, source_id, priority}`; `text` is plain text, control characters stripped, <= 280 chars (longer is truncated at a word boundary with an ellipsis). Items from several sources are ordered by the crawl's `order` setting (`round_robin` or `grouped`).

### Alert (FR-017 to FR-024)

| Field | Type | Rules |
|---|---|---|
| `id` | str | Caller-supplied `ext_id` or generated; re-push with the same id updates in place |
| `severity` | enum | `info`, `warning`, `critical` (anything else rejected) |
| `title` | str \| null | <= 80 chars, plain text |
| `message` | str | 1..280 chars, plain text, control characters stripped |
| `source` | str \| null | <= 40 chars |
| `duration_s` | int | Defaults: info 10, warning 20, critical 60; capped at `max_duration_s` (default 300) |
| `received_at` | float | Wall-clock when accepted (CLI or spool receive time) |
| `shown_at` | float \| null | When it first appeared on the panel; the countdown starts here |
| `ended_at` | float \| null | |
| `state` | enum | See state machine |
| `outcome` | enum \| null | `dismissed`, `resolved`, `timed_out`, `expired_in_queue` |

**State machine**

```text
            push
              |
              v
          [queued] --(max_wait elapsed / queue overflow / elapsed-while-down)--> [expired_in_queue]*
              |  ^
   (slot free)|  |(preempted by strictly higher severity; re-queued, not missed)
              v  |
         [displayed] --(duration elapsed)--> [timed_out]*
              |
              +--(dismiss cmd)--> [dismissed]
 any state --(resolve cmd, same id)--> [resolved]
```

`*` = counted in the missed-alert indicator. `dismissed` and `resolved` are never counted. A resolve for an id already counted as missed removes it from the missed count.

**Queue rules**: ordered by severity (critical > warning > info) then `received_at`; strictly higher severity preempts the displayed alert; queue cap 50 (overflow drops the oldest `info`, then `warning`, recorded as `expired_in_queue`); `max_wait_s` default 120.

**Display**: one alert is shown at a time over the whole main region; a corner badge shows `+N queued`.

### Missed Alert and indicator (FR-024, FR-041)

`missed_alerts` rows: `{id, severity, title, message, received_at, outcome}`. The indicator topic `alerts.missed` has `{count, max_severity}`. It is cleared only by `alert clear-missed` (all or by id) or by a matching resolve. It is independent of history retention.

### History retention (clarified)

`history.retention` is one of `1h` (default), `2h`, `4h`, `today`. Entries older than the window, measured from `received_at` (for `today`: before local midnight), are deleted by a housekeeping pass at least every 60 s. Missed alert rows are not deleted by retention.

### Sidebar Entry (FR-037 to FR-041)

| Field | Type | Rules |
|---|---|---|
| `id` | str | Unique |
| `icon` | bundled icon name or path to PNG | Unusable image falls back to a generic icon |
| `label` | str | <= 12 chars, optional |
| `topic` | str | Bus topic carrying an integer count |
| `hide_when_zero` | bool | Default true |
| `max_display` | int | Default 999; larger counts show `999+` |
| `stale_after_s` | int \| null | Chip shows `?` when stale |

Limit: <= 6 entries including the built-in alert entry (72 px minimum each). The alert entry is implicit, bound to `alerts.missed`, colored by `max_severity`, and hidden at zero.

### Control request and result

See [control-protocol.md](contracts/control-protocol.md). Every response is `{id, ok, data}` or `{id, ok:false, error:{code, message, setting?}}`.

### Spool entry

JSON file `{v, received_at, cmd, args}` named `<received_at_ns>-<uuid>.json`; ownership of the file is the caller's identity for the access check.

## Storage mapping

| Entity | Storage |
|---|---|
| Config, layout, instances, sources | `config.json` (atomic) |
| Secrets | `secrets.json` (0600) |
| Alerts (history) | The alerts plugin's private SQLite database (`<home>/state/<instance>.db`, table `alerts`) |
| Missed alerts | Same private database, table `missed_alerts` |
| Pushed values, counts, CLI crawl items | Core `state.db`, table `topics` (value, ts, ttl) |
| Gauge samples, crawl item cache, plugin state | memory only |
| Spool entries | files in the spool directory |
| Log | rotating JSON-lines files (1 MB x 5) plus an in-memory ring of the last 200 events for `status` and `logs` |

Core `state.db` tables: `topics(topic PK, value_json, ts, ttl_s)` and `meta(key PK, value)`. The alerts plugin's private database holds `alerts(id PK, ext_id, severity, title, message, source, duration_s, received_at, shown_at, ended_at, state, outcome)` and `missed_alerts(id PK, ext_id, severity, title, message, received_at, outcome)`, with a schema version in its own `meta` table. Keeping plugin tables out of the core database is what lets the alerts plugin use only the public SDK.
