# Contract: `smart-panel` CLI

The CLI is the only control surface (FR-002, FR-004). It talks to the runner over the [control protocol](control-protocol.md) and, for push-type commands only, falls back to the spool when the runner is down.

## Global behavior

```text
smart-panel [--home DIR] [--json] [--quiet] [--timeout SECONDS] <command> [args]
```

| Flag | Meaning |
|---|---|
| `--home DIR` | Override `SMART_PANEL_HOME` |
| `--json` | Machine-readable output (see envelope). Also enabled by `SMART_PANEL_JSON=1` |
| `--quiet` | Suppress human-readable output; exit code only |
| `--timeout S` | Control-channel timeout, default 5 |

### JSON envelope (FR-005)

Success, written to stdout:

```json
{"ok": true, "data": { ... }}
```

Failure, written to stdout (not stderr) so agents parse one stream; human mode writes the message to stderr:

```json
{"ok": false, "error": {"code": "invalid_input", "message": "settings.speed must be between 10 and 600", "setting": "plugins.crawl.settings.speed"}}
```

Push commands that were spooled return `{"ok": true, "data": {"saved_for_later": true, "spool_id": "..."}}` with exit code 0.

### Exit codes (documented, stable)

| Code | Name | Meaning |
|---|---|---|
| 0 | `ok` | Success (including `saved_for_later`) |
| 1 | `error` | Unexpected internal error |
| 2 | `invalid_input` | Usage error, validation failure, unknown plugin or setting |
| 3 | `runner_not_running` | Command needs the live runner and none is running |
| 4 | `panel_unavailable` | Runner is up but the panel is not connected (only for commands that require it, such as `brightness` verification with `--verify`) |
| 5 | `not_permitted` | Caller is not the runner's account and not on the allow-list (FR-051) |
| 6 | `conflict` | A runner is already running, or a resource is in use |
| 7 | `plugin_error` | A plugin command failed or its instance is unhealthy |

`error.code` strings match the names above plus `not_found` (exit 2), `spool_full` (exit 1), `timeout` (exit 1), and `protocol_mismatch` (exit 1).

## Core commands

### Runner lifecycle (FR-002, FR-003, FR-008, FR-013)

| Command | Behavior |
|---|---|
| `run [--sink hid\|files\|null] [--files-dir DIR]` | Run the runner in the foreground (used by the service) |
| `start [--sink ...]` | Start in the background, or via the service manager if installed. Exit 6 if already running |
| `stop [--timeout S]` | Stop the runner and release or blank the panel |
| `restart` | Stop then start; waits until the first frame is sent or the timeout expires |
| `reload` | Re-read `config.json` from disk and apply it (validated; last good config kept on error) |
| `status` | Runner state, uptime, panel state, measured fps, target fps and governor level, active layout, every plugin instance's health, recent errors, spool depth |
| `service install \| uninstall \| status` | systemd user unit (Linux) or launchd agent (macOS) |
| `logs [--follow] [--lines N] [--level L]` | Tail the log via the runner |

`status --json` data (stable fields):

```json
{"state": "running", "uptime_s": 1234, "pid": 4242, "version": "0.1.0",
 "panel": {"state": "connected", "sink": "hid", "last_error": null, "reconnects": 0},
 "frames": {"fps": 14.8, "target_fps": 15, "governor_level": 0, "last_bytes": 31544, "quality": 70},
 "layout": {"regions": [{"name": "main", "instance": "gauges"}]},
 "plugins": {"gauges": {"state": "ok", "last_error": null}},
 "spool": {"pending": 0}, "errors": []}
```

### Configuration (FR-004, FR-006, FR-007)

| Command | Behavior |
|---|---|
| `config show [PATH]` | Print the config or a dotted path (secrets redacted) |
| `config get PATH` | Print one value |
| `config set PATH VALUE` | Set a value (`VALUE` parsed as JSON, else string). Validated and applied live, then persisted |
| `config unset PATH` | Reset to default |
| `config validate [--file F]` | Validate without applying |
| `config schema [--plugin NAME]` | Emit the JSON schema of the core config and plugin settings |
| `config path` | Print the config file location |
| `secret set NAME [--stdin]` | Store a secret (never echoed) |
| `secret list` / `secret delete NAME` | Names only |

### Display controls

| Command | Behavior |
|---|---|
| `brightness set N` | 0-100 (FR-011); `0` turns the backlight off. Applied immediately and persisted |
| `brightness get` | |
| `layout show` | |
| `layout set --file F` or `layout region set NAME --x --y --w --h --instance I` | Replace or edit regions |
| `layout overlay add REGION INSTANCE` / `layout overlay remove REGION INSTANCE` | |
| `preview [--output FILE.png]` | Render the current composed canvas to a PNG (works with no panel) |

### Plugins (FR-042, FR-046)

| Command | Behavior |
|---|---|
| `plugin list` | Available plugins (built-in and discovered), version, capabilities |
| `plugin describe NAME` | Manifest: settings with types, ranges, defaults; commands; capabilities |
| `plugin instance list` | Configured instances and health |
| `plugin instance add NAME --plugin P [--set K=V ...]` | Create an instance |
| `plugin instance set NAME K=V [K=V ...]` | Change settings |
| `plugin instance enable NAME` / `disable NAME` / `remove NAME` | |
| `plugin call INSTANCE VERB [--arg K=V ...] [--json-args JSON]` | Generic route to any plugin command |

### Access (FR-051)

| Command | Behavior |
|---|---|
| `access list` | Allowed users and group |
| `access allow-user NAME` / `deny-user NAME` | Runner account or root only |
| `access set-group NAME` / `clear-group` | Runner account or root only |

### Discovery for agents

| Command | Behavior |
|---|---|
| `schema` | One JSON document describing every command, argument, exit code, config key, and plugin verb, so an agent can discover the whole surface (SC-008) |
| `version` | Package and protocol versions (no permission required) |

## Plugin verbs (generated from manifests)

These are shorthand for `plugin call` on the default instance of each built-in plugin. The CLI builds them from plugin manifests, so third-party plugins gain verbs the same way. Details and argument types: [builtin-plugins.md](builtin-plugins.md).

| Group | Verbs |
|---|---|
| `alert` | `push`, `dismiss`, `resolve`, `list`, `clear-missed`, `show` |
| `crawl` | `items set`, `items add`, `items clear`, `source add`, `source set`, `source remove`, `source list`, `preset list` |
| `gauge` | `add`, `set`, `remove`, `list`, `push` |
| `sidebar` | `entry add`, `entry set`, `entry remove`, `entry list`, `count set` |
| `data` | `push TOPIC VALUE [--ttl S]`, `get TOPIC`, `list` (generic bus access from the `sources` plugin) |

Push-type verbs (`alert push/dismiss/resolve/clear-missed`, `crawl items *`, `gauge push`, `sidebar count set`, `data push`) are marked `spoolable` in their manifests and succeed with `saved_for_later` when the runner is down.

### Example: the agent pushes an alert

```text
$ smart-panel --json alert push --severity warning --title "Disk" --message "nas01 at 91%" --id nas01-disk --ttl 30
{"ok": true, "data": {"id": "nas01-disk", "state": "queued", "position": 0}}
```

## Behavior guarantees

- Every command accepts `--json` and honors the envelope, including argument errors (exit 2).
- No command prints secrets. `config show` replaces secret values with `"***"`.
- Commands that change settings return the resulting effective value and `applied: true|false`.
- Commands are idempotent where it makes sense (`alert push` with the same `--id` updates; `instance enable` on an enabled instance succeeds).
