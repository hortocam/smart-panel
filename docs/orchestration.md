# Orchestration notes

How to run the work in `specs/001-status-display-platform/` with an orchestrator, Engineer agents, and Reviewer agents. Read `CLAUDE.md` first, then this file, then `tasks.md`. Environment prerequisites are in [hermes-setup.md](hermes-setup.md).

## Roles

| Role | Does | Must not |
|---|---|---|
| Orchestrator | Reads this file and `tasks.md`, creates one Kanban card per unit of work, sequences cards by the dependency rules below, routes hardware work over A2A, tracks PRs. | Write product code, merge its own cards, or mark hardware tasks done without a recorded result. |
| Engineer | Runs `/speckit-implement` scoped to the card's task IDs, opens a PR to `main`. | Review its own work, merge, or push to `main`. |
| Reviewer | Runs `/speckit-converge` against the PR, checks the constitution, and approves or returns the card. Must use a model of a different lineage from the Engineer that produced the PR. | Edit the Engineer's branch, or review a PR from its own lineage. |
| Hardware agent (on the Pi) | Runs hardware-marked tasks against the real panel and reports results. | Run anything not named in the card, or send byte sequences the card does not list. |

## Orientation order

1. `CLAUDE.md` (project facts, gotchas, commands).
2. `.specify/memory/constitution.md` (non-negotiable; version 1.1.0 or later).
3. `specs/001-status-display-platform/spec.md`, `plan.md`, `tasks.md`.
4. `research.md`, `data-model.md`, `contracts/`, `quickstart.md` as needed by a card.
5. `handoff/panel_protocol_handoff.md` before touching `panel_driver/` or anything that writes to the device.

## Unit of work and Kanban cards

`tasks.md` has 133 tasks in phases. Use the phase as the card for phases that are mostly sequential, and the user story as the card where stories can run in parallel.

| Card | Tasks | Notes |
|---|---|---|
| P1 Setup and CI | T001 to T015, T129, T133 | One PR for T001 to T014 and the T015 checkpoint. T129 (hardware) and T133 (owner action) are separate sub-cards. The P1 PR merges first; the owner then applies branch protection (T133) after the first green CI run, and P2 does not start until protection is verified active on `main`. |
| P2 Foundational | T016 to T035 | One PR, or split along file boundaries listed in `tasks.md` `[P]` markers. Blocks everything after it. |
| US1 Runner and CLI | T036 to T062 | The MVP. T130 (hardware) gates T062. |
| US2 Alerts | T063 to T073, T131, T132 | After US1. |
| US3 Crawl and sources | T074 to T089 | After US1. T081 unblocks US4. |
| US4 Gauges | T090 to T101 | After US1 and T081. |
| US5 Sidebar | T102 to T109 | After US1. End-to-end test T104 needs US2. |
| US6 Plugin authoring | T110 to T116 | After US1; T110 needs all built-ins. |
| Polish and Pi | T117 to T128 | T117 to T122 are software; T123 to T127 need the Pi; T128 is final verification. |

Checkbox state in `tasks.md` is the source of truth for progress. Engineers tick their own task boxes in their PR; the Reviewer verifies them.

### Parallelism

- After US1 merges, US2, US3 and US5 can run at once; US4 starts once T081 is merged.
- Conflict hotspots when running cards in parallel (serialize merges, or rebase before review):
  - `smart_panel/plugins/__init__.py` (registry edits in T061, T072, T088, T100, T109).
  - `pyproject.toml`, `smart_panel/core/plugins.py` and `smart_panel/core/workers.py` (T132).
  - `tasks.md` itself (checkbox edits); keep each PR to its own task lines.
- Do not split a card so that two agents edit the same file at the same time. `[P]` in `tasks.md` already means different files.

## Spec Kit mechanics for parallel agents

- Spec Kit resolves the feature from `.specify/feature.json` (`specs/001-status-display-platform`), not from the branch name, so card branches such as `001-p1-setup-ci` are safe.
- Set `SPECIFY_FEATURE_DIRECTORY=specs/001-status-display-platform` and `SPECIFY_FEATURE_NO_PERSIST=1` in every agent's environment so concurrent runs never rewrite `.specify/feature.json`.
- The git extension registers optional auto-commit hooks. Agents commit manually per task or logical group and decline the optional prompts unless the orchestrator says otherwise.

## Per-card flow

1. Orchestrator creates the card with: task IDs, the files named in those tasks, the dependency cards that must be merged, and the done criteria below. Branch name: `NNN-<card>` such as `001-p1-setup-ci`, cut from up-to-date `main`.
2. Engineer implements with tests first: write the tests listed in the card, confirm they fail, then implement. Commit per task or logical group.
3. Engineer opens a PR to `main` that states what changed and why, how it was tested, and any protocol evidence. For hardware-facing changes, include the hardware note (below).
4. Reviewer runs `/speckit-converge` against the spec, plan and tasks, checks the constitution, and either approves or returns the card with findings. Converge may append new tasks to `tasks.md`; those become new cards or are added to the open card.
5. After approval and green CI, the PR is merged (one PR per phase or story; no direct pushes).
6. Orchestrator unblocks dependent cards.

The bootstrap PR that introduced the spec, plan, tasks and these docs has no CI yet and merges on owner review alone. Done criteria for every card: its tests pass with `pytest` (hardware tests are skipped by default), `ruff check .` is clean, docs changed with behavior, the task boxes are ticked, CI is green on Linux and macOS, and the Reviewer has approved.

## Repository rules that bind every agent

- `main` will be protected (constitution, Contribution Workflow): pull request required, CI checks required, no force pushes, applied to administrators. T133 applies the protection after the first green CI run; until then it is not enforced by the platform, so still use pull requests for every change and never push to `main`.
- Commits and PRs carry the attribution lines the session provides.
- Every `dev.write` keeps the `b"\x00"` prefix and stays inside `panel_driver/device.py`.
- No byte sequence the vendor capture does not show may run on a default path. Brightness `0` is unverified until T129 reports.
- Protocol facts must be traceable to the capture or a recorded hardware test. Update `handoff/panel_protocol_handoff.md` in the same PR as any change to `protocol.py` or `device.py`.
- `panel_driver` depends only on `hid` and `Pillow`. `psutil` and `defusedxml` belong to the `app` extra and are imported only from `smart_panel`.
- Built-in plugins import only from `smart_panel.sdk`; `tests/contract/test_builtin_imports.py` enforces this.
- Vendor software or firmware is never committed. Committed captures stay filtered to the panel's own traffic.

## Hardware tasks and the A2A handoff

The VM has no panel. These tasks run on the Raspberry Pi (or the dev Mac for T129) through the hardware agent:

| Task | What | Needs |
|---|---|---|
| T129 | Phase 1 smoke test: test pattern and stream for at least 10 minutes, brightness 0, 50, 100. | Panel, merged T007. |
| T130 | MVP smoke test: runner with the real sink, live config change, unplug and replug. | Panel, merged T051 and T129. |
| T123 to T127 | Pi throughput, content and alert latency, unplug and power-cycle recovery, 72 hour soak, service install and reboot. | Panel, Pi, working install. |
| T133 | Apply branch protection with `gh`. | Repository owner's explicit go-ahead. |

A hardware job message should contain: the card and task ID, the exact commit SHA to check out, the commands to run, the expected observations, the maximum duration, and what to send back. The result message should contain: hardware, OS, Python version, duration, pass or fail per command, raw logs, and anything unexpected. Engineers paste the result into the PR description (constitution Principle IV).

Stop and report, never improvise, when: the panel stops responding (a power cycle needs a person), a command not listed in the card seems necessary, a result contradicts the protocol doc, or the soak shows memory growth beyond the success criterion. Do not fake or skip hardware results; mark the card blocked.

## Escalate to the owner

- Any change to the constitution, spec requirements, or protocol facts.
- Hardware results that contradict the documented protocol.
- Branch protection, repository settings, secrets, or anything outward-facing.
- A Reviewer finding the Engineer disputes twice.
