# Contract: Control Protocol and Spool

Local-only channel between the CLI (or any client) and the runner.

## Transport

- Unix domain stream socket at `<runtime>/control.sock` (`<runtime>` = `$SMART_PANEL_RUNTIME`, else `/tmp/smart-panel`; identical for all accounts).
- Directory mode 0755, socket mode 0666. **Authorization is by kernel peer credentials, not file permissions** (research decision 6).
- UTF-8 JSON, one object per line (`\n` terminated); max line 1 MiB. A connection may carry many requests; the server answers in order.
- No network listener exists in this release (webhook intake is deferred).

## Handshake

The first line from the client must be `{"hello": {"v": 1, "client": "smart-panel-cli/0.1.0"}}`. The server replies `{"hello": {"v": 1, "server": "0.1.0", "plugins_rev": 17}}`. A major version mismatch yields `protocol_mismatch` and the server closes the connection. `plugins_rev` increments when the plugin set changes; the CLI uses it to refresh its cached subcommand tree.

## Request and response

```json
{"id": 7, "cmd": "config.set", "args": {"path": "display.brightness", "value": 60}}
```

```json
{"id": 7, "ok": true, "data": {"path": "display.brightness", "value": 60, "applied": true}}
```

```json
{"id": 7, "ok": false, "error": {"code": "invalid_input", "message": "display.brightness must be between 0 and 100", "setting": "display.brightness"}}
```

`error.code` is one of `invalid_input`, `not_found`, `not_permitted`, `conflict`, `panel_unavailable`, `plugin_error`, `spool_full`, `timeout`, `protocol_mismatch`, `internal`. The CLI maps them to exit codes per [cli.md](cli.md).

## Commands

| `cmd` | `args` | Notes |
|---|---|---|
| `runner.status` | | Same data as `status` |
| `runner.reload` | | Re-read config from disk |
| `runner.stop` | `{timeout_s?}` | Graceful shutdown |
| `config.get` / `config.show` | `{path?}` | Secrets redacted |
| `config.set` / `config.unset` | `{path, value?}` | Validate whole candidate, apply live, persist |
| `config.validate` | `{config}` | No side effects |
| `config.schema` | `{plugin?}` | |
| `secret.set` / `secret.delete` / `secret.list` | | Owner or root only |
| `display.brightness.set` / `get` | `{value}` | |
| `display.preview` | `{}` | Returns a PNG as base64 (`data.png_b64`) |
| `layout.*` | see cli.md | |
| `plugin.list` / `describe` / `instance.*` | | |
| `plugin.call` | `{instance, verb, args}` | Routes to a plugin command; returns its result or `plugin_error` |
| `access.list` / `allow_user` / `deny_user` / `set_group` / `clear_group` | | Mutations: owner or root only |
| `logs.tail` | `{lines?, level?, follow?}` | With `follow`, the server streams `{"event": "log", ...}` lines until the client closes |
| `schema.get` | | The machine-readable CLI surface |

Plugin verbs travel as `plugin.call`; a manifest declares for each verb: `name`, `args` (typed settings), `spoolable` (bool), `mutates_config` (bool), and a one-line `summary`.

## Authorization

For every connection the server obtains `(uid, gid)` from the kernel and the peer's supplementary groups via `os.getgrouplist`. `access.is_allowed` returns true when the uid is the runner's uid or 0, is listed in `access.allowed_users`, or the peer belongs to `access.allowed_group`. Otherwise the server writes one `not_permitted` response, logs `denied uid=<n>`, and closes. Commands that change the allow-list or secrets additionally require uid == runner uid or 0.

## Atomicity and ordering

- Commands from different connections are serialized through one command queue, giving a defined order when two arrive together (spec edge case).
- A `config.set` is validated and applied atomically: on any validation error nothing changes and the last good configuration stays in effect (FR-006). After a successful apply the file is written with a temp file plus rename.
- `plugin.call` has a 2 s deadline for mutating verbs by default; overrun returns `timeout` and the plugin is marked `degraded`.

## Spool (runner not running)

When connecting fails with `ENOENT` or `ECONNREFUSED`, the client may spool commands whose manifest marks them `spoolable` (and only those).

File location: `<runtime>/spool/<received_at_ns>-<uuid>.json`, created with mode 0644 in a sticky 1733 directory. The CLI never creates `<runtime>`; if it is missing, the owner's account spools to `<home>/spool-pending/` (same file format, imported on start), and any other account gets `runner_not_running` (exit 3). Non-owner accounts set `$SMART_PANEL_RUNNER_UID` so the CLI can tell, and so it can refuse a socket owned by anyone else.

```json
{"v": 1, "received_at": 1791412345.123, "cmd": "plugin.call", "args": {"instance": "alerts", "verb": "push", "args": {"severity": "critical", "message": "UPS on battery"}}}
```

On startup the runner, before its first frame:

1. Lists spool files in name order (receive-time order).
2. Rejects any file whose owner uid is not allowed under the access policy, deleting it and logging the rejection.
3. Rejects malformed or oversize (> 64 KiB) files.
4. Replays each accepted command through the normal command path with `received_at` preserved. Alerts older than their display duration are recorded `expired_in_queue` and counted as missed (see [data-model.md](../data-model.md)).
5. Deletes processed files. Pending count is visible in `status`. The directory is capped at 1,000 files; later pushes fail with exit code 1 and error `spool_full`.

If the runner starts while a client is mid-write, the client's temp name ends in `.tmp` and is ignored until renamed.
