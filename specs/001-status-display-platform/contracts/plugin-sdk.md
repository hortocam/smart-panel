# Contract: Plugin SDK (`smart_panel.sdk`)

The only API a plugin may import from this project. Built-in plugins obey the same rule (FR-043); a contract test fails the build if a built-in plugin imports anything from `smart_panel.core`. Names below are the contract; signatures are indicative and are finalized in Phase 1 with the SDK's own tests.

## Discovery

1. **Installed packages**: the entry-point group `smart_panel.plugins`; each entry point resolves to a `Plugin` subclass.
2. **Drop-in**: every `*.py` file and every package directory in `<home>/plugins/` is imported; each `Plugin` subclass found is registered. Import errors are caught and reported in `plugin list` as `load_failed` with the error; they never stop the runner.

Plugin names are lowercase `[a-z][a-z0-9_-]{1,31}` and unique; a drop-in plugin may not shadow a built-in.

## Manifest

Every plugin class has a class attribute `manifest = Manifest(...)`:

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Unique plugin name |
| `version` | str | Plugin version (semantic) |
| `sdk_version` | str | SDK major version the plugin targets (currently `"1"`) |
| `summary` | str | One line, shown in `plugin list` |
| `capabilities` | set | Any of `region`, `overlay`, `datasource` |
| `settings` | list of `Setting` | Declared settings (below) |
| `commands` | list of `Command` | CLI verbs (below) |
| `source_types` | list of `SourceType` | Only for `datasource`: types of data source it can create |
| `animated` | bool | True if the plugin must be rendered every frame (for example a crawl) |
| `min_size` / `max_size` | (w, h) \| None | Region size limits; the layout is rejected if violated |

### Setting

`Setting(name, type, default, description, min=None, max=None, choices=None, pattern=None, secret=False, item=None)` with `type` in `int`, `float`, `bool`, `str`, `color`, `enum`, `list`, `object`, `duration`. The runner validates settings before they reach the plugin, so plugin code receives a dict that already conforms. `color` accepts `#RRGGBB`, `#RRGGBBAA`, or a CSS-style name from a small table. Secret settings accept only `${secret:NAME}` references.

### Command (CLI verb)

`Command(name, summary, args=[Setting...], spoolable=False, mutates_config=False)`; handled by `handle_command(name, args) -> dict`. Arguments are validated against `args`. The CLI renders these as `smart-panel <plugin> <verb> ...` and as `plugin call`.

## Lifecycle

```text
load -> instantiate(ctx, settings) -> start() -> [render()/handle_command()/on_settings_changed()]* -> stop()
```

| Method | Called | Contract |
|---|---|---|
| `__init__(self, ctx: InstanceContext, settings: dict)` | At create | Must be cheap; no I/O |
| `start(self)` | After create and after layout is applied | May start background work through `ctx.spawn` only |
| `stop(self)` | On remove, disable, or shutdown | Must return within 2 s; background work is cancelled via `ctx` |
| `on_settings_changed(self, settings: dict) -> None` | After a validated change | Return normally to accept; raise `SettingsRejected` to refuse (the old settings stay) |
| `render(self, frame: FrameContext) -> RenderResult` | Region and overlay plugins | See below |
| `handle_command(self, name: str, args: dict) -> dict` | CLI verb | Returns a JSON-compatible dict; raises `CommandError(code, message)` for expected failures |
| `describe_state(self) -> dict` | `status` and `plugin describe` | Small JSON-compatible health summary; no secrets |

Exceptions are caught by the runner. `degraded` means errors that did not prevent rendering; `stalled` means a render exceeded the hard deadline.

## Render contract (FR-014, FR-040, FR-045)

`FrameContext` provides: `size` (w, h in pixels), `now` (monotonic seconds), `wall` (epoch seconds), `frame_index`, `fonts` (`fonts.get(name_or_path, size)` with the configured fallback chain), `icons` (`icons.get(name, size)`), `bus` (read-only topic access, see below), `min_text_px`.

`RenderResult` is one of:

| Value | Meaning |
|---|---|
| `Unchanged()` | Reuse the previous image for this region (cheap path; the default for static plugins) |
| `Image(img)` | A new `PIL.Image` in RGBA or RGB, exactly `frame.size`; a wrong size is treated as invalid output |
| `Inactive()` | Overlay only: draw nothing |

Rules:

- Render must be a pure function of plugin state and `frame`; never block on the network. Fetching belongs in data sources or `ctx.spawn` workers.
- Time-based animation must use `frame.now`, never frame counts, so dropped frames do not cause jitter.
- Soft deadline 250 ms (logged), hard deadline 2 s (worker marked `stalled`; the last good image remains; the runner does not call it again until it returns).
- Invalid output (wrong type or size, exception) leaves the previous image in place, marks the instance `degraded`, and draws a small error badge in the region's corner.
- Text must stay at or above `frame.min_text_px`.

## Context services (`InstanceContext`)

| Service | Contract |
|---|---|
| `ctx.name`, `ctx.region_size` | Instance name and assigned size (if any) |
| `ctx.log` | Structured logger; secrets are redacted by the runner |
| `ctx.bus` | `get(topic) -> Sample`, `publish(topic, value, ttl_s=None)`, `subscribe(topic, callback)`, `topics(prefix)` |
| `ctx.kv` | Namespaced key/value persistence (JSON values, <= 64 KiB each) for small state |
| `ctx.db()` | A private stdlib `sqlite3` connection to `<home>/state/<instance>.db` (WAL mode) for plugins that need tables, such as alert history; the plugin owns its schema and migrations |
| `ctx.spawn(fn, interval_s=None)` | Run a background callable in the runner's worker pool; one at a time per task; timeouts enforced; cancelled on `stop` |
| `ctx.http` | Bounded HTTP GET helper (timeout, 2 MB cap, redirects limited, secrets substituted); no other network access is provided |
| `ctx.clock` | `monotonic()` and `time()`; replaced by a fake clock in tests |
| `ctx.request_render()` | Mark the region dirty |
| `ctx.notify(event)` | Emit an event to the log and `status` (for example `source_unreachable`) |

## Data source plugins

A `datasource` plugin declares `SourceType(name, settings, metrics)` and implements `fetch(self, source_name, settings) -> dict[str, Sample]` (called by the runner on the source's interval with a timeout). Returned samples are published to topics named `<source>.<metric>`. Raising an exception marks the source unhealthy; consumers see the last value become `stale` after its TTL.

## Testing without a panel (FR-047)

`smart_panel.sdk.testing` provides:

- `PluginHarness(plugin_class, settings, size=(w, h), clock=FakeClock())` with `.render(advance_s=0) -> PIL.Image | None`, `.command(name, **args)`, `.bus`, `.set_settings(...)`, `.state()`.
- `assert_conforms(plugin_class)`: the standard conformance suite (manifest valid, settings validate, commands declared, render returns allowed types and correct size, `stop` is prompt, no forbidden imports). Every built-in and sample plugin runs it in CI.
- `FakeClock` for deterministic time (animations, timeouts, aggregation windows).

## Compatibility

The SDK is versioned (`sdk_version`). Within a major version, additions are backward compatible; removals or semantic changes require a major bump and a deprecation period of one minor release. The runner refuses plugins targeting a newer major version and lists them as `incompatible`.

## Limits of isolation

Isolation is in-process (threads with deadlines). It contains exceptions, slow renders, and invalid output. It cannot stop a plugin that spins the CPU or holds the GIL, or terminate a stuck thread; such a plugin is reported `stalled` and a runner restart clears it. Plugin code runs with the runner's privileges, so only install plugins you trust.
