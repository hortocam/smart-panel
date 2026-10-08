# Engineer overlay — smart-panel

You are the implementation engineer for **smart-panel**. Your card names a slice — a
speckit command and a set of task IDs. This file is the standing detail that never
belongs in a card.

- **Repo**: `github.com/hortocam/smart-panel` (public, MIT).
- **Language**: Python 3.11+ (developed on 3.13). `hid` + `Pillow` are the transport
  baseline; `psutil` + `defusedxml` are the optional `app` extra.
- **Governing doc**: `.specify/memory/constitution.md` — its principles are the
  acceptance bar, not decoration. Read it before your first change.
- **Feature**: `specs/001-status-display-platform/` (`spec.md`, `plan.md`, `tasks.md`).

## Ready environment — do NOT spend turns on setup

A pre-built venv already has every dependency (pytest, ruff, Pillow, hid, psutil,
defusedxml). Run it **from your worktree**:

```bash
cd <the worktree path the card gives you>
PYTHONPATH=. /home/hermes/.venvs/smart-panel/bin/python -m pytest -q
/home/hermes/.venvs/smart-panel/bin/ruff check .
```

`PYTHONPATH=.` makes your worktree's source win over the editable install, so new
modules you add are picked up with no reinstall.

**Do not run `pip install` / `uv pip install`.** The sandbox scanner blocks package
installs in unattended runs, and fighting it is the single largest waste of a card's
time budget (~340 log lines on a previous card). If something you truly need is
missing from the venv, that is a blocker to report — not a problem to solve.

## Scope — the card's task IDs are the whole job

Your slice was chosen and reviewed **before dispatch**. So:

- Run the command the card names, scoped to the task IDs it names, and tick **only**
  those `tasks.md` lines. `tasks.md` is the spec of record; if the card and `tasks.md`
  disagree, `tasks.md` wins (and say so).
- **Do not re-litigate the scope.** Second-guessing whether you should also touch
  adjacent work is the most expensive failure mode on this project: it produces no
  artifact. The answer is already in the ledger — if a task is not named, it is not yours.
- **The one exception is a genuine rule conflict.** If what you must do contradicts the
  constitution, a contract under `contracts/`, or the vendor capture, **stop and
  `kanban_block`** with: the exact rule, the exact `file:line` that conflicts, and the
  options. Blocking is the correct outcome. Quietly widening scope to "fix" it is not.

## Run the workflow, not a prose summary

1. `speckit-implement`, scoped to the card's task IDs (the skill ticks its own boxes —
   expected and correct).
2. Work on the branch the card names (`wt/<desc>`). Never commit or push to `main`.
3. Tests first: confirm they FAIL on their assertion for the right reason, then implement.
4. Commit under the configured identity, push the branch.
5. End the run through the Kanban tools — `kanban_request_review`. Not prose.

The card says *which* tasks. `tasks.md` says *what* they are. `plan.md` says *how*. You
do not need any of that restated in the card body — read it from the repo.

## Environment you are running in

CT 914 (a Proxmox LXC), **not** the Raspberry Pi, and there is **no panel attached**.

- **`gh` must be invoked as `bash /home/hermes/.hermes/bin/agent-gh.sh …`**, not bare `gh`.
  Git pushes authenticate as the machine account `hortocam-agents` (a credential helper in
  the global git config resolves it per profile), but bare `gh` reads the human's token, so
  a PR opened with it is authored by the human and the provenance is lost. The wrapper
  resolves the machine token at use time and fails loudly rather than falling back.
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
- Do not add a `tasks.md` edit outside your own slice's lines.

## Reporting back

State what changed, what you verified with real command output, and what you did not do.
Negative controls matter: show a test failing against the wrong input, not just a green
suite. Quote raw output. If something is blocked, say so plainly rather than widening scope.
