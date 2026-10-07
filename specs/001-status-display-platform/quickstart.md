# Quickstart: Validating the Status Display Platform

End-to-end checks that prove the feature works. Each scenario names the user story and success criteria it covers. Details live in the [CLI](contracts/cli.md), [control protocol](contracts/control-protocol.md), [plugin SDK](contracts/plugin-sdk.md), and [built-in plugin](contracts/builtin-plugins.md) contracts, and entities in [data-model.md](data-model.md).

Scenarios A to H run **without a panel** (the file sink) and are the basis of the automated integration tests. Scenarios P1 to P4 need the physical panel on a Raspberry Pi 4B and are recorded as hardware verification notes in pull requests (constitution Principle IV).

## Prerequisites

```bash
# Raspberry Pi OS 12 / macOS, Python 3.11+
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Linux only: let your account open the panel (see README for the udev rule)
sudo cp packaging/99-smart-panel.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger

export SMART_PANEL_HOME=$(mktemp -d)      # isolated state for testing
pytest -m "not hardware"                  # unit, contract, integration
```

## No-panel scenarios (file sink)

All use `--sink files --files-dir "$SMART_PANEL_HOME/frames"`; the latest composed frame is `frames/latest.png` and `smart-panel preview` renders on demand.

### A. Run and manage (US1; FR-001 to FR-013; SC-001, SC-004, SC-008)

```bash
smart-panel --json start --sink files --files-dir "$SMART_PANEL_HOME/frames"
smart-panel --json status                       # state: running, plugins ok, panel.sink: files
smart-panel plugin instance add hello --plugin text --set text=Hello
smart-panel layout region set main --instance hello      # temporarily show it in the main region
smart-panel preview --output /tmp/a.png         # shows "Hello" in under 5 s
smart-panel --json start                        # exit code 6: already running
smart-panel config set display.fps 500 ; echo $?   # exit 2, names display.fps, running config unchanged
smart-panel restart && smart-panel --json status   # running again, first frame within 10 s
smart-panel stop
```

Expected: exit codes 0, 0, 0, 0, 6, 2, 0, 0; every command's `--json` output has the envelope; `status` lists each plugin's health.

### B. Alerts (US2; FR-017 to FR-024; SC-002)

```bash
smart-panel alert push --severity critical --title "UPS" --message "On battery" --id ups-1
smart-panel preview --output /tmp/b1.png        # alert covers main; sidebar and crawl visible
sleep 61 && smart-panel --json alert list        # ups-1 outcome timed_out
smart-panel preview --output /tmp/b2.png        # sidebar alert entry shows count 1
smart-panel alert clear-missed                  # count returns to 0
smart-panel alert push --severity info --message x ; smart-panel alert push --severity bogus --message y ; echo $?   # exit 2
smart-panel alert resolve --id ups-1            # resolved wherever it is
```

Time-dependent steps use the fake clock in the automated tests (no real sleeps).

### C. Pushes while the runner is down (clarified behavior)

```bash
smart-panel stop
smart-panel --json alert push --severity warning --message "while down"   # exit 0, saved_for_later: true
smart-panel --json status ; echo $?             # exit 3
smart-panel start                               # replays the spool; the alert is shown or, if older than its duration, recorded expired and counted missed
```

### D. Crawl (US3; FR-025 to FR-030; SC-003)

```bash
smart-panel crawl items set "Alpha headline" "Beta headline" "Gamma headline"
smart-panel plugin instance set crawl fg=#FFFFFF bg=#B00020 speed_px_s=200
smart-panel crawl source add --id news --kind rss --preset google-news-topic --topic TECHNOLOGY
smart-panel crawl source add --id nfl --kind json --preset espn-nfl
smart-panel --json status                        # each source: last_fetch, last_error
```

Automated checks use recorded fixtures for the Google News and ESPN responses, plus an unreachable source to verify last-known-items fallback.

### E. Gauges (US4; FR-031 to FR-036)

```bash
smart-panel plugin call sources source-add --name sys --type system --metrics cpu,mem,temp,disk:/
for g in cpu mem temp disk; do smart-panel gauge add --id $g --label ${g^^} --topic sys.$g --aggregation avg --window 60 --style dial; done
smart-panel gauge add --id q --label Queue --topic push.q --aggregation max --window 300 --style bar
smart-panel gauge push q 42
smart-panel gauge add --id g6 ... ; smart-panel gauge add --id g7 ... ; echo $?   # seventh rejected, exit 2, message names the maximum of 6
```

### F. Sidebar (US5; FR-037 to FR-041)

```bash
smart-panel sidebar entry add --id mail --icon mail --label Mail --topic count.mail
smart-panel sidebar count set mail 1204          # chip shows 999+
smart-panel sidebar count set mail 0             # chip hidden (hide-when-zero)
```

### G. Plugin extensibility (US6; FR-043 to FR-047; SC-009)

```bash
cp examples/plugins/clock.py "$SMART_PANEL_HOME/plugins/"   # sample third-party plugin (drop-in)
smart-panel plugin list                          # clock listed, no core changes
smart-panel plugin instance add c1 --plugin clock && smart-panel layout overlay add ...
smart-panel plugin call c1 crash ; smart-panel --json status   # c1 degraded, other regions still render
```

The conformance suite (`sdk.testing.assert_conforms`) runs against every built-in and the sample plugin, and a contract test asserts built-ins import only `smart_panel.sdk`.

### H. Access control (FR-051)

Run the integration test that starts the runner under one uid and connects as another (skipped when unprivileged); the pure policy function is unit tested in all environments. Manually: `smart-panel access allow-user hermes`, then run `alert push` as `hermes` (accepted); as a third account (exit 5). Allowed non-owner accounts export `SMART_PANEL_RUNNER_UID=<runner uid>`; with it unset or wrong, a socket owned by another uid is refused (impersonation guard), and with the runtime directory missing (runner never started since boot) the push exits 3 rather than creating it.

## Hardware scenarios (Raspberry Pi 4B + panel)

| # | Check | Pass criteria |
|---|---|---|
| P1 | `scripts/bench_throughput.py` (Phase 0) with the test pattern | Reports packets per second, frame time, and sustainable fps; update the handoff doc |
| P2 | Start with the default layout and the Scenarios D, E, F data | Content visible within 15 s (SC-001); crawl at 10 fps or more (SC-003) |
| P3 | Unplug and replug the panel; separately power-cycle it | Runner stays up; content returns within 10 s of availability (SC-007) |
| P4 | 72 h soak with a load stand-in for the agent | No crash; RSS growth under 10% after hour 1 (SC-006); CPU under 50% of one core and RSS under 500 MB (SC-005) |

Record device, OS, Python version, and duration for each in the pull request.

## Done checklist

- [ ] Scenarios A to H pass in CI on Linux and macOS.
- [ ] `pytest -m "not hardware"` is green; CI green on Phase 0 before any feature merge.
- [ ] Hardware scenarios P1 to P4 recorded.
- [ ] `smart-panel schema` output covers every command used above (SC-008).
