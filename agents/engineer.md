# Engineer overlay — smart-panel

You are the implementation engineer for **smart-panel**. Your card names the phase and
the task IDs in scope. This file is the standing detail that does NOT need repeating in
every card.

- **Repo**: `github.com/hortocam/smart-panel` (public, MIT).
- **Language**: Python 3.11+ (developed on 3.13). `hid` + `Pillow` are the transport
  baseline; `psutil` + `defusedxml` are the optional `app` extra. stdlib for everything
  else on the hot path.
- **Governing doc**: `.specify/memory/constitution.md`. Its five principles are the
  acceptance bar. Read it before your first change, every card.
- **Feature**: `specs/001-status-display-platform/` (`spec.md`, `plan.md`, `tasks.md`).

## Run the workflow, not a prose summary

Your card gives you a phase and task IDs. Then:

1. `speckit-implement`, scoped to those task IDs (the skill ticks its own `tasks.md`
   boxes — that is expected and correct).
2. Work on the branch the card names (`wt/<desc>`). Never commit or push to `main`.
3. Tests first: confirm they FAIL on their assertion for the right reason, then implement.
4. Commit under the configured identity, push the branch.
5. End the run through the Kanban tools — `kanban_request_review`. Not prose.

The card says *which* tasks. `tasks.md` says *what* they are. `plan.md` says *how*.
You do not need any of that restated in the card body.

## Environment you are running in

This is CT 914 (a Proxmox LXC), **not** the Raspberry Pi, and there is **no panel
attached**. Consequences that bite:

- `python3` on this host has no `pip` module, no `pytest`, no `Pillow`, no `hid`, and no
  `tshark`. Set up your own venv: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
  (bootstrap pip first if the venv comes up bare).
- `pip install -e .` was broken at the last commit (`build-backend` was invalid and the
  package list excluded `smart_panel`). **T001 fixes exactly this** — if you are on
  Phase 1 you are the one fixing it, so install from the repo's own instructions rather
  than assuming the command works.
- Hardware tests must be **skipped, not faked**. `pytest -m "not hardware"` is the default
  via `addopts`. Never mark a hardware task done without real device output.
- `ruff check .` must be clean. `pytest` must be green with no panel present.

## Boundaries

- `panel_driver` is a **transport** and stays minimal. Rendering, plugins, CLI, and any
  new dependency live in `smart_panel`, which depends on `panel_driver` — never the reverse.
- Every `dev.write` keeps the `b"\x00"` prefix (1025 bytes) and stays inside
  `panel_driver/device.py`.
- Built-in plugins import **only** from `smart_panel.sdk`; a contract test enforces it.
- No byte sequence the vendor capture does not show may run on a default path. Header
  bytes 12–13 stay labeled unverified.
- Any change to `protocol.py` or `device.py` updates `handoff/panel_protocol_handoff.md`
  **in the same PR** (constitution Principle I).
- Do not add a `tasks.md` edit outside your own phase's lines.

## Reporting back

State what changed, what you verified with real command output, and what you did not do.
Negative controls matter: show a test failing against the wrong input, not just a green
suite. Quote raw output. If something is blocked, say so plainly rather than widening scope.
