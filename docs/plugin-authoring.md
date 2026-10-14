# Plugin authoring

A **plugin** adds content to the smart-panel display (or supplies the data that
feeds it) without touching the core. The built-ins — `text`, `alerts`, `crawl`,
`sources`, `gauges`, `sidebar` — use exactly the same contract that is documented
here (FR-043), and every one of them is checked against the SDK conformance
suite in CI.

The only API a plugin may import is **`smart_panel.sdk`**. Importing
`smart_panel.core` fails `tests/contract/test_builtin_imports.py`, and the SDK
suite checks the same thing for a third-party plugin. The reference example is
[`examples/plugins/clock.py`](../examples/plugins/clock.py); this guide walks
through it.

- [Where plugins live](#where-plugins-live)
- [A minimal region plugin](#a-minimal-region-plugin)
- [The manifest](#the-manifest)
- [Settings](#settings)
- [Lifecycle](#lifecycle)
- [The render contract](#the-render-contract)
- [Commands (CLI verbs)](#commands-cli-verbs)
- [Context services](#context-services)
- [Data-source plugins](#data-source-plugins)
- [Alert-source plugins](#alert-source-plugins)
- [Testing without a panel](#testing-without-a-panel)
- [Packaging as an entry point](#packaging-as-an-entry-point)
- [The limits of isolation](#the-limits-of-isolation)

## Where plugins live

A plugin is discovered two ways (contract
[`plugin-sdk.md`](../specs/001-status-display-platform/contracts/plugin-sdk.md)):

1. **Installed packages** — an entry point in the `smart_panel.plugins` group
   that resolves to a `Plugin` subclass (see
   [Packaging](#packaging-as-an-entry-point)).
2. **Drop-in** — every `*.py` file and every package directory in
   `<home>/plugins/` is imported and every `Plugin` subclass it defines is
   registered. `<home>` is `$SMART_PANEL_HOME` (default
   `~/.config/smart-panel`).

```bash
cp examples/plugins/clock.py "$SMART_PANEL_HOME/plugins/"
smart-panel --json plugin list          # the copied plugin appears
```

Plugin names are lowercase `[a-z][a-z0-9_-]{1,31}` and must be unique. A drop-in
may **not** shadow a built-in or another installed plugin; a name clash, a bad
name and a broken import are all reported as `load_failed` in `plugin list` and
never stop the runner.

## A minimal region plugin

```python
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw

from smart_panel.sdk.plugin import Manifest, Plugin
from smart_panel.sdk.plugin import Image as RenderImage


class HelloPlugin(Plugin):
    manifest = Manifest(
        name="hello",
        version="1.0.0",
        summary="Draws a greeting",
        capabilities=frozenset({"region"}),
    )

    def render(self, frame: Any) -> Any:
        image = PILImage.new("RGBA", frame.size, (0, 0, 0, 255))
        draw = ImageDraw.Draw(image)
        draw.text((10, 10), "hello", font=frame.fonts.get("DejaVuSans", 48))
        return RenderImage(image)
```

Assign it to a region by configuration alone — no core change:

```bash
smart-panel plugin instance add hello --plugin hello
smart-panel layout region set hello --x 0 --y 0 --w 1920 --h 462 --instance hello
```

The region's plugin renders at the region's size: `frame.size` is the region
rectangle, and an `Image` result whose size differs is invalid output.

## The manifest

Every plugin class has a class attribute `manifest = Manifest(...)`. `plugin
list` and `plugin describe` are built from it, and the runner validates settings
and sizes against it.

| Field | Type | Meaning |
|---|---|---|
| `name` | str | Unique plugin name, `[a-z][a-z0-9_-]{1,31}` |
| `version` | str | Plugin version (semantic) |
| `sdk_version` | str | SDK **major** the plugin targets; currently `"1"`. A newer major is listed `incompatible` |
| `summary` | str | One line, shown in `plugin list` |
| `capabilities` | set | Any of `region`, `overlay`, `datasource`, `alert_source`, `alert_sink` |
| `settings` | list of `Setting` | Declared settings (below) |
| `commands` | list of `Command` | CLI verbs (below) |
| `source_types` | list of `SourceType` | Only for `datasource`: the source kinds it can create |
| `animated` | bool | True if the plugin must be rendered every frame (a clock, a crawl) |
| `min_size` / `max_size` | (w, h) or None | Region size limits; the layout is rejected if violated |

## Settings

Declare every value the runner should validate and every value `plugin
describe`/`config schema` should show:

```python
from smart_panel.sdk.settings import Setting

SETTINGS = (
    Setting(name="format", type="str", default="%H:%M:%S",
            description="A strftime(3) pattern"),
    Setting(name="size", type="int", default=96, description="Text size in pixels",
            min=12, max=512),
)
```

`Setting(name, type, default, description, min=None, max=None, choices=None,
pattern=None, secret=False, item=None)`, with `type` one of `int`, `float`,
`bool`, `str`, `color`, `enum`, `list`, `object`, `duration`.

- `min`/`max` bound numeric values; `choices` lists an `enum`'s values;
  `pattern` is a regex for a `str`; `item` describes a `list`'s elements.
- `color` accepts `#RRGGBB`, `#RRGGBBAA` or a CSS-style name from a small table.
- `secret=True` accepts only a `${secret:NAME}` reference; the value is resolved
  by the runner and redacted from logs and errors.

**The runner validates settings before they reach the plugin** (FR-006), so your
code receives a dict that already conforms; a bad value is refused with the
constraint your `Setting` declares quoted:

```text
$ smart-panel --json plugin instance add c1 --plugin clock --set size=9999
{"ok": false, "error": {"code": "invalid_input",
 "message": "value 9999 is above the maximum 512",
 "setting": "plugins.c1.settings.size"}}
```

Settings are read through `self.settings` (always the validated mapping). A
setting you omit gets its declared default.

## Lifecycle

```text
load -> __init__(ctx, settings) -> start() -> [render()/handle_command()/on_settings_changed()]* -> stop()
```

| Method | Called | Contract |
|---|---|---|
| `__init__(self, ctx, settings)` | At create | Must be cheap; **no I/O** |
| `start(self)` | After create and after the layout is applied | May start background work **through `ctx.spawn` only** |
| `stop(self)` | On remove, disable or shutdown | Must return within **2 s**; cancel background work (the runner also cancels `ctx.spawn` tasks on `stop`) |
| `on_settings_changed(self, settings)` | After a validated settings change | Return normally to accept; `raise SettingsRejected(msg, setting=...)` to refuse (the old settings stay) |
| `render(self, frame)` | Region and overlay plugins, each frame | See below |
| `handle_command(self, name, args)` | A CLI verb | Return a JSON-compatible dict; `raise CommandError(code, message)` for expected failures |
| `describe_state(self)` | `status` and `plugin describe` | Small JSON health summary; never a secret |

## The render contract

`FrameContext` is the per-frame input:

| Field | Meaning |
|---|---|
| `size` | The region's `(width, height)` in pixels |
| `now` | Monotonic seconds |
| `wall` | Wall-clock POSIX seconds |
| `frame_index` | A monotonically increasing pass counter |
| `fonts` | `fonts.get(name_or_path, size)` — the configured fallback chain |
| `icons` | `icons.get(name, size)`; never raises, always a square RGBA image |
| `bus` | Read-only topic access: `get(topic)`, `topics(prefix)` |
| `min_text_px` | The configured minimum text size |

`render` returns one of:

| Result | Meaning |
|---|---|
| `Unchanged()` | Reuse the previous image for this region (the cheap path for static plugins) |
| `Image(img)` | A new `PIL.Image` in RGBA or RGB, **exactly `frame.size`** |
| `Inactive()` | **Overlay only**: draw nothing this frame (the layer is cleared) |

Rules:

- **Use `frame.now`** (or `frame.wall`) for anything time-based — never a frame
  count and never the process clock, so dropped frames cannot cause jitter and a
  fake clock can drive your render in tests.
- **Never block.** A render must be a pure function of your state and `frame`;
  network or disk I/O belongs in a data source or a `ctx.spawn` task.
- **Deadlines.** A render over the soft deadline (250 ms) is logged; one over the
  **hard deadline (2 s)** marks the instance `stalled`, keeps the last good image
  and draws an error badge. Time-based animation must not assume it is called at
  a fixed rate.
- **Invalid output** (a wrong type or size, or an exception) keeps the previous
  image, marks the instance `degraded`, records a `last_error` and draws a small
  error badge in the region's corner. Other regions are unaffected (FR-045).
- Keep text at or above `frame.min_text_px`.
- A region plugin has **no `Inactive` empty state**: draw your own background
  even when you have nothing to say, so the region does not show the canvas
  background instead of your `bg`.

## Commands (CLI verbs)

A command declared in the manifest becomes a CLI verb and a `plugin call` target.
The verb is handled by `handle_command(name, args)`:

```python
from smart_panel.sdk.plugin import Command, CommandError
from smart_panel.sdk.settings import Setting

manifest = Manifest(
    name="clock",
    commands=(
        Command(name="crash", summary="Make render() raise"),
        Command(
            name="set",
            summary="Set the format",
            args=(Setting(name="format", type="str", default="%H:%M:%S"),),
            spoolable=True,
        ),
    ),
)
```

`Command(name, summary, args=[Setting...], spoolable=False, mutates_config=False)`.
Argument values arrive in `args` already coerced to the declared types. Raise
`CommandError(code, message)` for an expected failure; `code` should be a
documented error code such as `invalid_input` or `not_found`.

Verbs are reachable three ways:

```bash
smart-panel clock crash                       # the plugin's own group
smart-panel clock set --format "%H:%M"        # declared args become --options
smart-panel plugin call c1 crash              # generic route to an instance
```

A verb marked **`spoolable=True`** is a push: if the runner is down, the CLI
spools it and the command succeeds with `{"saved_for_later": true, "spool_id":
"..."}` (exit 0). The runner replays the spool *before the first frame* on the
next start. Mark only verbs that are safe to apply later.

## Context services

`self.ctx` (the `InstanceContext`) is the long-lived service bag:

| Service | Contract |
|---|---|
| `ctx.name`, `ctx.region_size` | The instance name and its assigned size, if any |
| `ctx.log` | Structured logger: `ctx.log.info("event", key=value)`; secrets are redacted |
| `ctx.bus` | `get(topic) -> Sample`, `publish(topic, value, ttl_s=None)`, `subscribe(topic, cb)`, `topics(prefix)` |
| `ctx.kv` | Namespaced key/value persistence (JSON values, ≤ 64 KiB each) for small state |
| `ctx.db()` | A private stdlib `sqlite3` connection to `<home>/state/<instance>.db` (WAL); you own the schema |
| `ctx.spawn(fn, interval_s=None)` | Run `fn` in the runner's worker pool; one at a time; timeouts enforced; cancelled on `stop` |
| `ctx.http` | A bounded HTTP GET helper (timeout, 2 MB cap, redirects limited, `${secret:NAME}` substituted) |
| `ctx.clock` | `monotonic()` and `time()`; replaced by a fake clock in tests |
| `ctx.request_render()` | Mark your region dirty (a subscribed topic changed) |
| `ctx.alert(severity, message, ...)` | Raise an alert; requires the `alert_source` capability |
| `ctx.notify(event)` | Emit an event to the log and `status` |

A `Sample` has `.value`, `.ts` and `.freshness` (`fresh`, `stale`, `no_data`), so
a consumer can render "no data" rather than a stale number.

## Data-source plugins

A plugin with the `datasource` capability fetches outside data and publishes it
to the bus, so region plugins read a fresh value without ever blocking.

```python
from smart_panel.sdk.plugin import Manifest, Plugin, SourceType
from smart_panel.sdk.settings import Setting


class Counts(Plugin):
    manifest = Manifest(
        name="counts",
        capabilities=frozenset({"datasource"}),
        source_types=(
            SourceType(
                name="counts",
                settings=(Setting(name="url", type="str", default=""),),
                metrics=("open",),
            ),
        ),
    )

    def fetch(self, source_name: str, settings: dict) -> dict:
        """Return ``{metric: value}``; called on the source's interval, with a timeout.

        Raising marks the source unhealthy; consumers see the last value age to
        ``stale`` after its TTL. Do the network read through ``ctx.http`` (the one
        bounded fetch helper) and keep the call short.
        """
        payload = settings.get("settings") or {}
        body = self.ctx.http.get(payload["url"]).json()
        return {"open": float(body["open"])}
```

The plugin's own scheduled loop (a `ctx.spawn` task started from `start`) calls
`fetch` and publishes each metric to the topic `<source>.<metric>`. The built-in
`sources` plugin works exactly this way: `smart-panel data source add ...` (or
the `sources` object in its settings) declares sources, and each one is fetched
on its interval and published. A consumer reads it with `frame.bus.get("<source>.<metric>")`
and checks `.freshness` before drawing the value.

## Alert-source plugins

A region or background plugin that needs to raise a notification declares the
`alert_source` capability and calls `ctx.alert(...)`. The core routes it to
whichever instance declares `alert_sink` (the built-in `alerts` plugin) with the
same validation as `alert push` — the core contains no alerts-specific logic:

```python
ctx.alert(
    "warning", "nas01 at 91%", title="Disk", source="counts", id="nas01-disk", ttl=30
)
```

`ctx.alert` raises `PermissionError` if the plugin does not declare
`alert_source`, and the routed call fails clearly when no `alert_sink` instance
is configured.

## Testing without a panel

`smart_panel.sdk.testing` is part of the public SDK (FR-047). It provides
`FakeClock`, `PluginHarness` and `assert_conforms`:

```python
from smart_panel.sdk.testing import FakeClock, PluginHarness, assert_conforms

from clock import ClockPlugin  # the drop-in module


def test_the_clock_draws_the_formatted_time() -> None:
    clock = FakeClock(start=0.0, wall=1_700_000_000.0)  # deterministic wall time
    harness = PluginHarness(ClockPlugin, {"format": "%H:%M:%S"}, size=(400, 120), clock=clock)
    try:
        image = harness.render()
        assert image is not None and image.size == (400, 120)
        assert harness.state() == {"format": "%H:%M:%S", "crashed": False}
        assert harness.command("crash") == {"crashed": True}
    finally:
        harness.stop()


def test_the_plugin_conforms_to_the_sdk() -> None:
    assert_conforms(ClockPlugin)
```

`PluginHarness(plugin_class, settings, size=(1920, 462), clock=FakeClock())`
builds the plugin against a real bus and a fake clock and exposes
`.render(advance_s=0)`, `.command(name, **args)`, `.bus`, `.set_settings(...)`,
`.state()` and `.stop()`.

`assert_conforms(plugin_class)` is the standard suite: the manifest is valid,
settings validate, commands are declared, `render` returns an allowed type at the
exact size, `stop` is prompt, and the plugin imports nothing from
`smart_panel.core`. Every built-in and the sample plugin run it in CI
(`tests/contract/test_builtin_conformance.py`).

You do **not** need the panel: the whole suite runs with the file sink.

## Packaging as an entry point

To ship a plugin as a package instead of a drop-in, declare the entry point in
your `pyproject.toml`:

```toml
[project.entry-points."smart_panel.plugins"]
counts = "my_package.counts:CountsPlugin"
```

Install the distribution and the loader resolves the class on the next scan:

```bash
pip install ./my-plugin
smart-panel --refresh --json plugin list
```

The entry point name is a label; the plugin's identity is `manifest.name`, which
must still be a legal, unique name.

## The limits of isolation

Isolation is **in-process** (threads with deadlines), which contains exceptions,
slow renders and invalid output — the failure affects only that region. It
cannot stop a plugin that spins the CPU or holds the GIL, and it cannot terminate
a stuck thread; such a plugin is reported `stalled` and a runner restart clears
it. Plugin code runs with the runner's privileges, so **only install plugins you
trust** (contract
[`plugin-sdk.md`](../specs/001-status-display-platform/contracts/plugin-sdk.md),
"Limits of isolation").
