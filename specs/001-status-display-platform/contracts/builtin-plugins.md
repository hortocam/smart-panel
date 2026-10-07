# Contract: Built-in Plugins

Settings and commands for the plugins that ship with the project. All of them use only [`smart_panel.sdk`](plugin-sdk.md). Setting types and ranges are enforced at validation time (FR-006). Colors accept `#RRGGBB` or `#RRGGBBAA`.

## `text` (region)

Static text, used for first-run verification (US1) and as the reference sample plugin.

| Setting | Type | Default | Notes |
|---|---|---|---|
| `text` | str | `"smart-panel"` | <= 280 chars |
| `fg` / `bg` | color | `#FFFFFF` / `#000000` | |
| `font` | str | `"DejaVuSans"` | bundled name or TTF path |
| `size` | int | 48 | >= `min_text_px` |
| `align` | enum | `center` | `left`, `center`, `right` |

Commands: `set --text T` (spoolable).

## `alerts` (overlay on `main`)

Implements FR-017 to FR-024. Queue, preemption, and states per [data-model.md](../data-model.md). Declares the `alert_sink` capability: alerts raised by other plugins through `ctx.alert(...)` (capability `alert_source`) are routed here by the core and validated like `alert push`.

| Setting | Type | Default | Notes |
|---|---|---|---|
| `durations` | object | `{info: 10, warning: 20, critical: 60}` (seconds) | Each 1..300 |
| `max_duration_s` | int | 300 | Upper cap for any alert |
| `max_wait_s` | int | 120 | Max time in queue before an alert is counted as missed |
| `queue_cap` | int | 50 | 5..200 |
| `history.retention` | enum | `1h` | `1h`, `2h`, `4h`, `today` |
| `colors` | object | info `#2E7DD7`, warning `#E6A700`, critical `#D62839` | Background per severity; text color chosen for contrast |
| `font` / `title_size` / `message_size` | str / int / int | bundled / 56 / 40 | >= `min_text_px` |

Commands (CLI group `alert`):

| Verb | Arguments | Spoolable | Result |
|---|---|---|---|
| `push` | `--severity {info,warning,critical}` `--message M` `[--title T] [--source S] [--id ID] [--ttl SECONDS]` | yes | `{id, state: "queued"\|"displayed", position}` |
| `dismiss` | `--id ID` or `--current` | yes | `{id, outcome: "dismissed"}` |
| `resolve` | `--id ID` | yes | `{id, outcome: "resolved", was: state}` |
| `list` | `[--state S] [--limit N] [--since DURATION]` | no | History within retention, newest first |
| `show` | | no | Current alert, queue, missed count |
| `clear-missed` | `[--id ID]` | yes | `{cleared: N}` |

Publishes bus topic `alerts.missed` = `{count, max_severity}` with no TTL.

Validation: `severity` outside the three levels, empty `message`, `message` > 280 chars (truncated with a warning in the result), or `ttl` < 1 are rejected (exit 2).

## `sources` (datasource)

Provides the source types below. Sources are created with `smart-panel plugin call sources source-add ...` or by editing `sources` in config.

| Source type | Settings | Metrics published |
|---|---|---|
| `system` | `metrics` (list of `cpu`, `mem`, `temp`, `disk:<path>`, `net_rx`, `net_tx`), `interval_s` (>= 1, default 2) | `<name>.cpu` (percent), `.mem` (percent), `.temp` (degrees C; `no_data` if unavailable), `.disk:<path>` (percent), `.net_rx` / `.net_tx` (bytes per second) |
| `pushed` | `ttl_s` (default 300) | Topics `push.<name>` set by `data push` |
| `http_json` | `url`, `list_path` (optional), `fields` (name to template), `interval_s` (>= 30), `headers` (values may be `${secret:NAME}`), `timeout_s` (<= 10) | `<name>.<field>` per field; list sources publish `<name>.items` |
| `rss` | `url`, `max_items` (default 30), `interval_s` (>= 30, default 300) | `<name>.items` (list of titles) |

Commands (group `data`, plus `source`): `data push TOPIC VALUE [--ttl S]` (spoolable), `data get TOPIC`, `data list`, `source add/set/remove/list`.

## `crawl` (region)

Implements FR-025 to FR-030.

| Setting | Type | Default | Notes |
|---|---|---|---|
| `fg` / `bg` | color | `#FFFFFF` / `#B00020` | |
| `font` | str | bundled bold | name or TTF path |
| `size` | int | 44 | >= `min_text_px` |
| `speed_px_s` | int | 140 | 20..600 |
| `direction` | enum | `left` | `left`, `right` |
| `separator` | str | `"  •  "` | <= 8 chars |
| `order` | enum | `round_robin` | `round_robin`, `grouped` |
| `show_source_labels` | bool | false | |
| `placeholder` | str | `"No headlines"` | Shown if there were never any items |
| `sources` | list | `[]` | Each `{id, kind: cli\|rss\|json, ...}` |

Source kinds: `cli` (items set through the CLI), `rss` (`url`, `max_items`, `interval_s`), `json` (`url`, `list_path`, `template`, `max_items`, `interval_s`, `headers`).

Commands (group `crawl`):

| Verb | Arguments | Spoolable |
|---|---|---|
| `items set` | `[--source ID] ITEM...` or `--stdin` (one per line) | yes |
| `items add` / `items clear` | `[--source ID] ITEM...` | yes |
| `source add` | `--id ID --kind {rss,json} --url URL [--preset NAME] [--template T] [--list-path P] [--interval S] [--label L]` | no |
| `source set` / `remove` / `list` | | no |
| `preset list` | | no |

Presets (data, `crawl/presets.json`): `espn-nfl`, `espn-nba`, `espn-mlb`, `espn-nhl`, `espn-epl`, `espn-ncaaf` (JSON; URL `https://site.api.espn.com/apis/site/v2/sports/<sport>/<league>/scoreboard`, `list_path: events`, a template such as `{competitions.0.competitors.1.team.abbreviation} {competitions.0.competitors.1.score} @ {competitions.0.competitors.0.team.abbreviation} {competitions.0.competitors.0.score} - {status.type.shortDetail}`) and `google-news-search` / `google-news-topic` (RSS; URL built from `--query` or `--topic`). Presets are plain configuration so they can be corrected without a code release.

Behavior: the strip is pre-rendered when the item set changes; items are swapped at the loop seam, never mid-scroll (US3 scenario 2); a failed refresh keeps the last items and sets the source's `last_error` in `status` (US3 scenario 3).

## `gauges` (region on `main`)

Implements FR-031 to FR-036; 1 to 6 gauges, laid out as a grid sized to the count (1x1 to 3x2).

Settings: `gauges` (list of Gauge, see [data-model.md](../data-model.md)), `layout` (`auto` or `{cols, rows}`), `bg` (color, default `#101418`), `font`, `value_size` (default 72), `label_size` (default 28); sizes below `min_text_px` are clamped up.

Commands (group `gauge`):

| Verb | Arguments | Spoolable |
|---|---|---|
| `add` | `--id ID --label L --topic T --aggregation A [--window S] --style ST [--unit U] [--min N --max N] [--warn N] [--crit N]` | no |
| `set` / `remove` / `list` | | no |
| `push` | `ID VALUE` (publishes to `push.<id>`; the gauge must use that topic) | yes |

Validation: more than 6 gauges is rejected with a message naming the maximum (spec scenario 5). A gauge whose topic is stale beyond `stale_after_s` shows its stale state (dimmed value with `no data`).

## `sidebar` (region)

Implements FR-037 to FR-041.

Settings: `entries` (list of Sidebar Entry), `bg` (default `#0B0E11`), `chip_fg` / `chip_bg` (default `#FFFFFF` / `#D62839`), `font`, `label_size` (default 24), `chip_size` (default 24); sizes below `min_text_px` are clamped up.

Commands (group `sidebar`):

| Verb | Arguments | Spoolable |
|---|---|---|
| `entry add` | `--id ID --icon ICON [--label L] --topic T [--hide-when-zero/--show-zero] [--max-display N]` | no |
| `entry set` / `remove` / `list` | | no |
| `count set` | `ID N` (sets a value on the entry's topic with a TTL) | yes |

Built-in alert entry: always present, bound to `alerts.missed`; icon `alert`; hidden at zero; chip color follows `max_severity`. Counts above `max_display` show `999+`; a count that changes gets a brief highlight (US5 scenario 2). Bundled icon names: `mail`, `calendar`, `chat`, `alert`, `check`, `cloud`, `server`, `git`, `bell`, `home`, `shield`, `clock`.

Account integrations (Gmail, calendar) are not part of this release (FR-042). The agent supplies counts with `sidebar count set` (for example `sidebar count set mail 12`), and a future account plugin can publish the same topics.
