# Feature Specification: Status Display Platform

**Feature Branch**: `001-status-display-platform`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "Project Vision: I purchased this USB bar display panel (HOTSPOTEK, 1920x462 landscape canvas) to use in my 10" rack or as a small status board under my main monitor. Four UX features: (1) news-style crawl, (2) gauge display, (3) real-time alerts display, (4) sidebar with app icons and count chips. Everything is plugin-style so more can be added later. A CLI makes it easy to configure and relaunch the runner, with all the levers an agent needs to configure and manage the display, including pushing events/alerts directly. Initial deployment is a Raspberry Pi 4B (4 GB) alongside a Hermes agent."

## Clarifications

### Session 2026-10-06

- Q: Which severity levels should an alert support, and how long should each stay on screen by default? → A: Three levels: `info` (10 s), `warning` (20 s), `critical` (60 s); all defaults configurable.
- Q: Where can the crawl get its headlines from in this release? → A: Items set through the CLI, RSS/Atom feeds (Google News), and JSON web endpoints (ESPN.com, per sport), with a way to map JSON fields to headline text.
- Q: If the agent pushes an alert, count or value through the CLI while the runner is stopped or restarting, should the push be remembered and shown once the runner is back, or fail with an error? → A: Remember everything pushed while the runner is down and apply it on startup, but drop alerts older than their display duration when the runner starts.
- Q: When an alert takes over the screen, should the crawl and the gauges stay partly visible, or should the alert cover everything except the sidebar? → A: The alert covers the main (gauge) area; the crawl keeps scrolling along the bottom and the sidebar stays visible.
- Q: Which accounts on the Raspberry Pi should be allowed to control the display through the CLI (push alerts, change settings, restart the runner)? → A: The service's own account by default, plus an optional list of additional accounts or a group that the owner configures.

### Session 2026-10-07

- Q: How long should alert history be kept? → A: A configurable retention window of 1 hour (default), 2 hours, 4 hours, or "today" (since local midnight).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run and manage the display from the command line (Priority: P1)

The owner (or their agent) installs the project on a Raspberry Pi, describes what the panel should show in a configuration, and starts a long-running "runner" that continuously drives the panel. From then on, they use a command-line tool to start, stop, restart, reload configuration, check status, and inspect or change settings, without ever editing code or touching the panel's hardware.

**Why this priority**: Nothing else is visible or useful until a runner keeps the panel alive and a person (or agent) can control it. This is the foundation every other story builds on, and it is the only story that is useful on its own: it turns the existing one-shot scripts into a managed, always-on service.

**Independent Test**: With a panel attached (or the runner's no-hardware output mode), run the start command with a minimal configuration containing a single static text region. The text appears on the panel, the status command reports the runner as healthy, a configuration change plus a reload shows the new text within seconds, and the stop command blanks or releases the panel cleanly.

**Acceptance Scenarios**:

1. **Given** a valid configuration and an attached panel, **When** the user runs the start command, **Then** the panel is initialized and begins showing the configured layout, and the status command reports the runner as running along with panel state and uptime.
2. **Given** a running display, **When** the user changes a setting through the CLI (for example a color or a layout region), **Then** the change appears on the panel without restarting the runner and without a visible blank gap.
3. **Given** a running display, **When** the user runs the restart command, **Then** the runner stops and restarts, and the panel shows the configured content again within 10 seconds.
4. **Given** a running display, **When** the panel is unplugged and plugged back in (or power-cycled), **Then** the runner detects the loss, keeps running, and resumes showing content automatically once the panel is available again.
5. **Given** an invalid configuration, **When** the user starts the runner or reloads configuration, **Then** the command fails with a clear message naming the offending setting, and a previously running display keeps showing its last good configuration.
6. **Given** an account that is neither the runner's own account nor on the owner's allowed list, **When** it runs any CLI command that controls the display, **Then** the command is refused with a clear "not permitted" result and a distinct exit code, and the display is unaffected; **and** an allowed account's commands work normally.
7. **Given** any CLI command, **When** it is run with the machine-readable output option, **Then** its result (including errors) is emitted in a stable structured format that an agent can parse reliably, with meaningful exit codes.

---

### User Story 2 - Show alerts prominently, including ones pushed by an agent (Priority: P2)

When something needs attention, an alert appears on the panel immediately and unmistakably. Alerts are pushed through the command line, which the Hermes agent uses directly and which any other local process (scripts, monitoring tools' notification hooks) can call. The panel has no touch, keyboard, or mouse, so nobody can dismiss an alert at the screen: every alert is shown for a bounded time and then clears itself. Alerts that were shown (or never got the chance to be shown) and then timed out are not forgotten: the sidebar keeps an alert icon with a count so the owner can see at a glance that alerts came in.

**Why this priority**: Real-time alerting is the main reason to put a status board in view, and the agent push path is the headline agent-specific capability. It depends only on the runner (User Story 1), so it is the most valuable next slice.

**Independent Test**: With the runner active, issue a single CLI command that pushes a critical alert with a message. The alert takes over the display's attention area within 2 seconds, remains visible for its default display time, and then the normal display returns with the sidebar alert icon showing a count of 1. List the alert history through the CLI and confirm the alert appears as timed out; clear the missed-alert count through the CLI and confirm the count disappears.

**Acceptance Scenarios**:

1. **Given** a running display, **When** an alert is pushed through the CLI with a severity and message, **Then** the alert is displayed prominently within 2 seconds, visually distinct by severity, and it covers the entire main (gauge) area while the sidebar and the crawl strip stay visible and keep updating, so the alert cannot be overlooked yet the rest of the display is not blanked.
2. **Given** an alert pushed without an explicit display duration, **When** it is shown, **Then** a default duration for its severity applies, and the countdown starts when the alert actually appears on the panel, not when it was received.
3. **Given** an alert on screen, **When** its display duration elapses and nothing dismissed it, **Then** it is removed, the previous display returns, and the alert is recorded as timed out in the missed-alert count on the sidebar.
4. **Given** no alert may stay on screen indefinitely, **When** a sender asks for an unusually long display duration, **Then** the duration is capped at a configurable maximum.
5. **Given** several alerts arrive close together, **When** they are active, **Then** none are silently lost: the highest severity is shown first and most prominently, the display indicates how many others are queued, and each queued alert gets its full display time once it is shown.
6. **Given** an alert that waits in the queue longer than a configurable maximum wait, **When** that wait expires, **Then** it is not shown, is recorded as missed, and is counted in the sidebar alert indicator.
7. **Given** the agent dismisses a displayed alert through the CLI (for example because it has dealt with the issue), **When** the command is received, **Then** the alert is removed immediately and is not counted as missed.
8. **Given** an alert has been resolved by its source (a local process or the agent reports the same alert identifier as resolved), **When** that report is received, **Then** the matching alert is cleared whether it is on screen, queued, or already counted as missed.
9. **Given** the sidebar alert indicator shows missed alerts, **When** the agent or user clears them through the CLI, **Then** the count resets to zero and the icon is hidden or returns to its idle state; **and** until cleared, the count persists across display cycles.
10. **Given** a malformed alert (missing message, or a severity other than `info`, `warning`, or `critical`), **When** it is submitted, **Then** it is rejected with a clear error and the display is unaffected.

---

### User Story 3 - News-style crawl (Priority: P3)

A horizontal ticker scrolls headlines or any list of text items across the panel, like the crawl on a TV news channel. The owner chooses the data source for the items and controls colors, speed, font, and size. Items refresh periodically without interrupting the scroll.

**Why this priority**: It is the owner's first-named feature and delivers a distinctly "newsroom" look, but it can be delivered and demoed independently once the runner exists.

**Independent Test**: Configure the crawl with a fixed list of five text items, a red-on-black color scheme, and a medium speed. The items scroll continuously and smoothly from right to left in a loop; changing the speed or colors via the CLI is reflected on the panel without a restart.

**Acceptance Scenarios**:

1. **Given** a crawl configured with a list of items, **When** the runner is active, **Then** the items scroll continuously across the crawl region in a seamless loop, in the configured colors, font, size, and speed.
2. **Given** a crawl bound to a changing data source (for example a Google News feed, an ESPN sport endpoint, or a list the agent updates), **When** the source's items change, **Then** the crawl picks up the new items on its next refresh without restarting or visibly jumping mid-scroll.
3. **Given** a crawl whose source is unreachable or returns nothing, **When** the refresh fails, **Then** the crawl keeps showing its last known items (or a configured placeholder if there never were any) and the failure is visible in status output.
4. **Given** a very long item or a very large number of items, **When** displayed, **Then** the crawl remains readable and bounded: long items are not cut off mid-character, and the scroll does not stall.
5. **Given** non-English text or symbols (accented characters, emoji, CJK), **When** present in an item, **Then** characters are rendered when the configured font supports them and replaced by a visible fallback otherwise, never causing a crash.

---

### User Story 4 - Gauge and graph display (Priority: P4)

The owner defines up to six gauges or small graphs, each with a data source, an aggregation rule (for example average over the last minute, or maximum over the last hour), a label, units, and thresholds that change its color. Typical uses: CPU load, memory, temperatures, disk or network usage, and values pulled from other systems.

**Why this priority**: It supplies the "at-a-glance system health" half of the status board. It builds on the runner and is independent of the crawl and alerts.

**Independent Test**: Configure four gauges backed by system metrics (CPU, memory, temperature, disk). Their values appear in a four-up layout and update at their configured rates; crossing a configured threshold changes a gauge's color. Add a fifth and sixth gauge and confirm the layout adapts; attempt a seventh and confirm it is rejected.

**Acceptance Scenarios**:

1. **Given** a configuration with N gauges (1 ≤ N ≤ 6), **When** the runner is active, **Then** all N are displayed legibly in the gauge region, each showing its label, current value, and units.
2. **Given** a gauge with an aggregation rule (average, min, max, last, or sum over a time window), **When** new samples arrive, **Then** the displayed value reflects the aggregation over the configured window, not just the latest sample.
3. **Given** a gauge with thresholds (for example warning at 75%, critical at 90%), **When** the value crosses a threshold, **Then** the gauge's color changes accordingly and returns when the value recovers.
4. **Given** a gauge's data source is unavailable or stale, **When** no fresh sample arrives within the gauge's staleness limit, **Then** the gauge visibly shows a "no data" or "stale" state instead of a misleading old value.
5. **Given** a configuration with more than six gauges, **When** it is validated, **Then** it is rejected with a message stating the maximum.
6. **Given** a gauge style choice (dial, bar, numeric, or small line graph with recent history), **When** configured, **Then** the gauge is drawn in that style.
7. **Given** a gauge whose source is a value pushed through the CLI, **When** the agent pushes a new value, **Then** the gauge updates within one refresh interval.

---

### User Story 5 - Sidebar with app icons and count chips (Priority: P5)

A narrow sidebar shows familiar application icons (for example mail, calendar, chat), each with a small count chip: unread messages, events today, open tickets, and so on. Each entry's count comes from a data source and updates on its own schedule; a count of zero can be hidden or shown according to preference.

**Why this priority**: It makes the panel useful as a personal "inbox glance" surface, but it is the most dependent on outside accounts and is therefore best delivered after the core experience works.

**Independent Test**: Configure three sidebar entries (mail, calendar, and a custom one) with counts pushed through the CLI. The icons and chips appear in the sidebar; pushing a new count updates the chip within one refresh interval; a count of zero follows the "hide when zero" setting.

**Acceptance Scenarios**:

1. **Given** a sidebar with configured entries, **When** the runner is active, **Then** each entry shows its icon, an optional short label, and a count chip, in the sidebar region; **and** a built-in alert entry shows the missed-alert count (see User Story 2).
2. **Given** an entry whose count changes, **When** the new value arrives, **Then** the chip updates, and a visible change indicator (for example a brief highlight) draws the eye to it.
3. **Given** a count of zero and the "hide when zero" preference, **When** displayed, **Then** the chip is hidden (or shown as "0" when the preference is off).
4. **Given** a count above a display limit (for example 1000), **When** displayed, **Then** it is abbreviated (for example "999+") and still fits within the chip.
5. **Given** an entry with a custom icon image, **When** configured, **Then** the custom icon is used; if no icon is provided or the image is unusable, a generic fallback icon is shown.
6. **Given** a count source is unavailable, **When** its data goes stale, **Then** the chip shows a stale/unknown state rather than a misleading number.

---

### User Story 6 - Extend the display with new plugins (Priority: P6)

A developer (or the owner's agent) can add a new kind of content, such as a new crawl source, a new gauge data source, a new alert source, or a new sidebar integration, by writing a self-contained plugin, without modifying the core runner. The four built-in features are themselves delivered the same way, proving the mechanism.

**Why this priority**: It is a design requirement that shapes every other story, but its direct value to a single owner is lower than the features themselves, so the extension experience is polished last.

**Independent Test**: Drop a minimal sample plugin (for example one that renders a clock) into the plugin location with no changes to core files. It appears in the list of available plugins, can be added to the layout through configuration, and renders on the panel. Removing it leaves the runner unaffected.

**Acceptance Scenarios**:

1. **Given** a plugin that follows the documented contract, **When** it is placed in the plugin location, **Then** the CLI lists it as available and it can be assigned to a layout region by configuration alone.
2. **Given** a plugin that crashes, hangs, or produces invalid output, **When** the runner is active, **Then** only that plugin's region is affected (shown as an error state) and every other region and the panel connection keep working.
3. **Given** a plugin that declares settings, **When** the user edits them through the CLI, **Then** unknown or invalid settings are rejected with a clear message based on the plugin's declared settings.
4. **Given** the plugin contract documentation, **When** a developer follows it, **Then** they can build and test a plugin without a physical panel.

---

### Edge Cases

- The panel is absent at startup: the runner starts anyway, reports "waiting for panel", and begins driving it as soon as it appears.
- The panel hangs or stops responding mid-session (a known behavior without proper initialization): the runner detects repeated write failures, reports them in status, and attempts recovery with backoff instead of spinning or exiting.
- Two CLI commands (for example an alert push and a config change) arrive at the same moment: both take effect, in a defined order, and neither corrupts the other.
- The runner is started twice: the second start is refused with a clear message instead of two runners fighting over one panel.
- The device's system clock changes or the machine suspends and resumes: schedules and time-window aggregations recover without error.
- Configuration references a font, icon, or file that does not exist: the item is replaced by a visible fallback and a warning appears in status output; the runner continues.
- Resource pressure on the shared Pi (the agent is busy): the runner lowers its frame rate or update rates rather than starving the agent, and reports that it has done so.
- An alert flood (for example 500 alerts in a minute): the display stays responsive, older low-severity alerts are collapsed into a count, and the oldest are discarded only after being recorded in a log.
- A single frame would exceed what the panel can accept: the runner automatically reduces image quality until it fits instead of failing.
- CLI input (alerts, crawl items, counts) contains very long text, control characters, or markup: it is displayed as plain text, truncated safely, and never executed or interpreted.
- Alerts pushed while the runner is down are replayed on startup in the order received, except that any alert whose display duration has already elapsed since it was received is not shown; it is recorded in the history as expired and counted in the missed-alert indicator, so it is never silently lost.
- An alert is counted as missed but its history entry has aged out of the retention window: the sidebar count still includes it until cleared, and the history listing no longer shows it.
- The runner restarts while alerts are active or missed: the missed-alert count and alert history survive the restart; alerts that were on screen are treated as timed out.

## Requirements *(mandatory)*

### Functional Requirements

**Runner and CLI**

- **FR-001**: The system MUST provide a long-running runner that continuously drives the panel with the composed display, re-sending frames as the panel requires.
- **FR-002**: The system MUST provide a command-line tool with commands to start, stop, restart, reload configuration, and report status of the runner.
- **FR-003**: The status command MUST report at minimum: runner state, uptime, panel connection state, current frame rate, active layout, each plugin's health, and recent errors.
- **FR-004**: The CLI MUST expose every setting that affects the display (layout, per-plugin settings, brightness, frame rate, colors, fonts, data sources) so that anything configurable can be inspected and changed through commands alone, with no need to hand-edit files.
- **FR-005**: Every CLI command MUST support a machine-readable output mode with stable field names, and MUST return distinct, documented exit codes for success, invalid input, runner not running, and panel unavailable. Push commands (alerts, counts, values, crawl items) are the exception to the "runner not running" failure: they MUST succeed with a result stating the push was saved for when the runner starts (see the alert and persistence requirements).
- **FR-006**: Configuration changes MUST be validated before being applied; invalid changes MUST be rejected with a message that identifies the setting, and the last good configuration MUST remain in effect.
- **FR-007**: Configuration changes made through the CLI MUST take effect on the running display without a full restart, and MUST persist across restarts. Counts, gauge values, and crawl items pushed while the runner is not running MUST be saved and applied when it starts.
- **FR-008**: The runner MUST prevent more than one instance from driving the same panel.
- **FR-009**: The runner MUST start and keep running when the panel is not attached, and MUST detect panel disconnection and reconnection automatically.
- **FR-010**: The runner MUST initialize the panel correctly at the start of every session and MUST adjust image quality automatically so that each frame fits the panel's size limit.
- **FR-011**: The system MUST allow setting panel brightness (0–100) at any time through the CLI, including turning the backlight off.
- **FR-012**: The system MUST support a no-hardware mode that renders the composed display to image files instead of the panel, so that layouts, plugins, and tests can be exercised without a panel attached.
- **FR-013**: The system MUST be able to run unattended on boot as a managed service on the Raspberry Pi, and the CLI MUST provide a way to install and remove that service configuration.

**Layout and rendering**

- **FR-014**: The system MUST compose the display on a 1920×462 landscape canvas, divided into named regions (such as sidebar, main area, crawl strip), each assigned to one plugin instance.
- **FR-015**: A default layout MUST be provided that places the sidebar on one side, the gauge area in the main region, and the crawl along the bottom edge, and the user MUST be able to override region placement and size through configuration. An alert MUST be displayed over the entire main region (the region that holds the gauges), covering its content for the alert's duration, and MUST leave the sidebar and crawl regions visible and updating.
- **FR-016**: The system MUST keep the display legible: text and values MUST remain readable at normal viewing distance for a 9.16" panel (a configurable minimum text size with a sensible default).

**Alerts**

- **FR-017**: The system MUST accept alerts containing at least a severity and a message, and optionally a title, source, display duration, and identifier. The supported severities are exactly `info`, `warning`, and `critical`; any other value MUST be rejected.
- **FR-018**: Alerts MUST be accepted through the CLI (including while the runner is not running, in which case they are saved and evaluated when it starts), which any process on the same machine (the agent, scripts, monitoring-tool notification hooks) can invoke. Network-facing alert intake (such as a webhook receiver) is out of scope for this feature.
- **FR-019**: Every alert MUST have a bounded display duration: a default per severity when none is given (`info` 10 s, `warning` 20 s, `critical` 60 s, each configurable), capped at a configurable maximum. Because the panel has no input device, no alert may depend on a person dismissing it at the panel.
- **FR-020**: Alerts MUST be displayed prominently and distinctly by severity, within 2 seconds of receipt when nothing higher-priority is showing, with no alert silently dropped (queued, collapsed, and missed alerts MUST all be indicated). The display duration countdown MUST start when the alert is actually shown, and a configurable maximum queue wait MUST apply to alerts waiting to be shown.
- **FR-021**: Alerts MUST be removable by expiration, by explicit dismissal through the CLI, or by a "resolved" report through the CLI that shares their identifier. Dismissed and resolved alerts MUST NOT be counted as missed.
- **FR-022**: The system MUST keep a history of received alerts, recording each one's outcome (dismissed, resolved, timed out, or expired in queue without being shown), that can be listed through the CLI, and MUST keep it across restarts. History entries MUST be retained for a configurable window of 1 hour (default), 2 hours, 4 hours, or "today" (since local midnight), measured from when each alert was received, and older entries MUST be removed automatically. The missed-alert count is independent of this window and persists until cleared (see the missed-alert requirement).
- **FR-023**: Alert text MUST always be treated as plain text.
- **FR-024**: Alerts that timed out or expired unseen MUST be counted in a missed-alert indicator, and the count MUST persist until cleared through the CLI.

**Crawl**

- **FR-025**: The system MUST scroll a list of text items horizontally across the crawl region in a seamless loop.
- **FR-026**: The crawl MUST allow configuring foreground color, background color, scroll speed, font, text size, item separator, and scroll direction.
- **FR-027**: The crawl MUST obtain its items from one or more configurable data sources, MUST refresh each on its own configurable interval without interrupting the scroll, and MUST fall back to last known items when a refresh fails.
- **FR-028**: The crawl MUST support these item sources in this release: items set through the CLI, RSS/Atom feeds (for example Google News topic and search feeds), and JSON web endpoints (for example ESPN.com endpoints for a specific sport). For JSON endpoints the user MUST be able to specify which fields form each headline (for example "away team, score, home team, game status"), and ready-made field mappings for common ESPN sport endpoints MUST be provided.
- **FR-029**: When several crawl sources are configured, the crawl MUST interleave or group their items in a configurable order, and MUST identify each item's source when the user chooses to show source labels.
- **FR-030**: Crawl items MUST be settable through the CLI, so an agent can supply headlines directly.

**Gauges**

- **FR-031**: The system MUST support between 1 and 6 gauges in the gauge region and MUST reject configurations with more than 6.
- **FR-032**: Each gauge MUST declare a label, a data source, a refresh interval, an aggregation rule with a time window, units, a value range, and optional color thresholds.
- **FR-033**: The system MUST provide the following gauge data sources out of the box: local system metrics (CPU load, memory, temperature, disk, network), values pushed through the CLI, and values read from a configured web endpoint that returns structured data.
- **FR-034**: The system MUST support at least these aggregations: last value, average, minimum, maximum, and sum, each over a configurable time window.
- **FR-035**: Gauges MUST support at least these styles: dial, horizontal bar, numeric, and short history line graph.
- **FR-036**: Gauges MUST show an explicit stale or no-data state when their source stops reporting for longer than a configurable limit.

**Sidebar**

- **FR-037**: The system MUST display a configurable list of sidebar entries, each with an icon, an optional label, and a count chip.
- **FR-038**: The system MUST include a set of standard application icons (including mail, calendar, and chat) and MUST allow custom icon images.
- **FR-039**: Count values MUST be settable through the CLI, and sidebar entries MUST also support pulling counts from a configured data source on a refresh interval.
- **FR-040**: The sidebar MUST support hide-when-zero, count abbreviation above a limit, and a stale/unknown state.
- **FR-041**: The sidebar MUST include a built-in alert entry (alert icon with a count chip) that shows the missed-alert count, takes on the color of the highest severity among missed alerts, and is hidden or idle when the count is zero.
- **FR-042**: This release MUST NOT include built-in integrations that sign in to external accounts (such as Gmail or calendar services). Sidebar counts come from the CLI (supplied by the agent or any local process) or from a generic configured data source; the plugin contract MUST allow an account-specific count plugin to be added later without changes to the core.

**Plugins**

- **FR-043**: The system MUST deliver the crawl, gauges, alerts display, and sidebar as plugins that use the same extension contract available to third parties; the core MUST NOT contain feature-specific logic for them.
- **FR-044**: A plugin MUST be able to declare its settings, supply content for a region, and (where it needs outside data) supply data sources, alert sources, or both.
- **FR-045**: The system MUST isolate plugins so that a plugin failure, slowness, or invalid output affects only that plugin's region, and MUST report the failure in status output.
- **FR-046**: The CLI MUST list available plugins and their declared settings, and MUST allow enabling, disabling, and configuring plugin instances.
- **FR-047**: The plugin contract MUST be documented well enough to build and test a plugin without a physical panel.

**Resource use and reliability**

- **FR-048**: The system MUST run acceptably on a Raspberry Pi 4B with 4 GB of RAM while a separate agent process runs on the same machine, and MUST degrade its own frame rate and refresh rates under load rather than starving other processes.
- **FR-049**: Credentials and secrets (such as data source tokens) MUST NOT be written to logs or shown in status output, and MUST be stored with restrictive file permissions.
- **FR-050**: The system MUST log notable events (start/stop, panel loss and recovery, configuration changes, plugin failures, alert receipt) in a form that can be viewed through the CLI.
- **FR-051**: Commands that control the display (everything except read-only help and version output) MUST be permitted only to the account that runs the runner and to any additional accounts or group the owner has configured; every other account MUST be refused with a distinct, documented exit code. By default no additional accounts are allowed. The allowed list MUST be changeable only by the runner's own account (or an administrator).

### Key Entities *(include if feature involves data)*

- **Runner**: The long-running process that owns the panel connection, composes the display, and applies configuration. One per panel.
- **Layout**: The arrangement of named regions on the 1920×462 canvas and the plugin instance assigned to each.
- **Region**: A rectangular area of the canvas with a position, size, and one assigned plugin instance (for example sidebar, main, crawl).
- **Plugin**: A self-contained extension that provides content for a region and/or data or alerts. Declares its name, settings, and capabilities.
- **Plugin Instance**: A configured use of a plugin in a region, with its own settings and health state.
- **Data Source**: A provider of values or lists (system metrics, CLI-pushed values, web endpoints, plugin-provided sources) with a refresh interval and freshness state.
- **Gauge**: A displayed measurement with label, data source, aggregation rule and window, units, range, style, and thresholds.
- **Crawl Item**: One piece of text in the scrolling ticker, with optional source label and priority.
- **Crawl Source**: A provider of crawl items: CLI-set list, RSS/Atom feed, or JSON endpoint with a field mapping, each with its own refresh interval and freshness.
- **Alert**: A notification with identifier, severity, message, optional title/source, display duration, and state (queued, displayed, dismissed, resolved, timed out, expired in queue).
- **Missed-Alert Indicator**: The persistent count of alerts that timed out or expired unseen, shown on the sidebar until cleared.
- **Sidebar Entry**: An icon with optional label and a count chip, bound to a count value and its freshness. The alert indicator is a built-in entry.
- **Configuration**: The persisted, validated set of all settings, changeable through the CLI.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: From a fresh Raspberry Pi with the project installed, a user can go from the start command to content visible on the panel in under 15 seconds, and from a restart command to content visible in under 10 seconds.
- **SC-002**: An alert pushed through the CLI is visible on the panel within 2 seconds in at least 95% of attempts.
- **SC-003**: The crawl scrolls at the configured speed without visible stutter, with the displayed frame rate staying at or above 10 frames per second during continuous scrolling.
- **SC-004**: A configuration change made through the CLI is visible on the panel within 5 seconds, with no restart required.
- **SC-005**: With all four built-in features active (6 gauges, crawl, sidebar with 5 entries, and an alert shown), the display consumes no more than 50% of one Raspberry Pi 4B CPU core on average and no more than 500 MB of memory, leaving the agent responsive.
- **SC-006**: The runner operates continuously for at least 72 hours without crashing, and its memory use at the end of that period is within 10% of its use one hour after start.
- **SC-007**: After the panel is unplugged and reconnected, content is back on the panel within 10 seconds of the device becoming available, with no user action.
- **SC-008**: Every display-affecting setting can be viewed and changed using the CLI alone; an agent can configure a complete display from an empty setup using only CLI commands and machine-readable output.
- **SC-009**: A sample third-party plugin can be added and displayed with zero changes to core files, and a deliberately failing plugin leaves every other region functioning.
- **SC-010**: 100% of requirements covering layout, plugin behavior, configuration validation, alert handling, gauge aggregation, and CLI output can be verified by automated tests that run on a machine with no panel attached.

## Assumptions

- The CLI is the display's only control surface, and access to it is limited to the runner's account plus any accounts the owner explicitly allows; on a multi-user machine, other logins are refused.
- A single panel is driven by a single runner; driving several panels is out of scope for this release.
- The existing driver (USB transport, protocol framing, rotation, init sequence) is the foundation and is not redesigned here. New work sits above it, consistent with the project constitution's layering principle.
- The display is landscape-oriented at 1920×462; portrait or other orientations are out of scope.
- The first deployment target is a Raspberry Pi 4B (4 GB) running a Linux-based OS; macOS is also supported for development and testing.
- The agent (Hermes) interacts with the display only through the CLI; no other agent integration path is required in this release. Anything running on the same machine can push alerts, counts, values, and crawl items by calling the CLI.
- A network-facing alert intake (webhook receiver) is deferred to a future feature. Local processes that want to forward monitoring alerts do so by invoking the CLI.
- Playing arbitrary video files is out of scope for this release; animation (scrolling, gauge updates, alert flashing) produced by plugins is in scope.
- The panel is not touch-enabled and the driving device has no keyboard or mouse, so all control happens through the CLI and nothing on screen waits for input.
- Alerts take over the main region (covering the gauges) while the sidebar and the crawl stay visible and continue to update. Every alert, critical included, times out; default durations are 10 s (`info`), 20 s (`warning`), and 60 s (`critical`), and are configurable.
- Built-in Gmail and calendar integrations are out of scope. Mail and calendar counts are supplied through the CLI (for example by the agent) in this release; a dedicated account plugin may follow later.
- Public web sources (Google News feeds, ESPN.com endpoints) are used as published, for personal non-commercial use. The ESPN.com JSON endpoints are not an officially documented interface and may change without notice; the crawl's last-known-items fallback and status reporting are the mitigation, and field mappings are configuration so they can be fixed without code changes.
- Data from external accounts, however supplied, is limited to counts; message content is never displayed or stored.
- The display is shared with people nearby, so sensitive details (message subjects, senders) are not shown on the sidebar.
- A small set of standard icons is bundled; additional icons are user-supplied.
- Configuration is stored locally on the machine running the runner; remote or cloud sync of configuration is out of scope.
- Continuous integration and the supporting test scaffolding are established in Phase 0 of the plan, before feature work.
