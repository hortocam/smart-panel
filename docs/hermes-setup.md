# Hermes setup prerequisites

Checklist for the two Hermes instances that build and verify this project. It lists what the repository needs from them; how Hermes is configured is up to the owner. Items marked **Confirm** are assumptions the owner should verify.

## Topology

```
Hermes VM (orchestrator, Engineer, Reviewer)  --A2A-->  Raspberry Pi (local Hermes, hardware agent, panel attached)
        |                                                         |
        +-- GitHub: branches, PRs, CI (Linux and macOS) ----------+
```

The VM never touches the panel. The Pi runs only hardware jobs the orchestrator sends.

## Hermes VM

- [ ] Clone of `hortocam/smart-panel` with push access to feature branches and permission to open PRs. It does not need admin rights; branch protection is applied by the owner.
- [ ] `git` identity set, and commits include the attribution lines the session provides.
- [ ] `gh` authenticated (read CI results, open PRs, comment on PRs).
- [ ] Python 3.11 or newer, with `pip install -e ".[dev]"` working in a venv. Optional: `uv` for the agent-context refresh in `CLAUDE.md`.
- [ ] `tshark` installed (T004 extracts test fixtures from `handoff/fullpaneltest.pcapng`).
- [ ] No hid access needed. All VM tests run with file and null sinks and skip the `hardware` marker.
- [ ] Spec Kit skills available: `/speckit-implement`, `/speckit-converge`, `/speckit-analyze`, `/speckit-tasks`.
- [ ] The Engineer and Reviewer run on models of different lineage. The existing Hermes hooks must reject a review requested from the same lineage as the PR's author. **Confirm** that the hook identifies lineage from the model, not the agent name.
- [ ] Hooks link each card to the speckit command it must run (implement for Engineer, converge for Reviewer) and to the PR.
- [ ] The orchestrator reads `CLAUDE.md` and `docs/orchestration.md` from `main` to orient. After the first PR merges, `main` contains both.

## Raspberry Pi 4B

- [ ] Raspberry Pi OS 64-bit (Bookworm, Python 3.11) with the panel attached (VID/PID `5548:1011`).
- [ ] Repository checkout the hardware agent can switch to a given commit SHA.
- [ ] `libhidapi-hidraw0` installed, and `pip install -e ".[dev]"` working.
- [ ] udev rule from `packaging/99-smart-panel.rules` installed (created in T010), and the service account in the `plugdev` group. Reload with `udevadm control --reload && udevadm trigger`, then replug the panel.
- [ ] For T127: `loginctl enable-linger <user>` and permission to install a systemd user service.
- [ ] The Pi's Hermes instance runs locally on the Pi with cloud models. It must not have credentials to push to `main` or change repository settings.
- [ ] A person can power-cycle the panel when asked. A hung USB controller cannot be recovered by software.
- [ ] Enough free space and a way to ship logs back (soak CSVs, runner logs) with the A2A reply.

## A2A contract (proposed; **Confirm** against the current configuration)

Orchestrator to hardware agent:

```json
{
  "card": "US1",
  "task": "T130",
  "commit": "<sha>",
  "commands": ["pip install -e '.[dev]'", "smart-panel start", "..."],
  "expect": ["frame rate at least 10", "content visible within 15 s"],
  "max_duration_s": 900,
  "report": ["hardware", "os", "python", "duration", "per-command result", "logs"]
}
```

Hardware agent to orchestrator: the same `card` and `task`, plus `status` (`pass`, `fail`, `blocked`), the fields named in `report`, and a short free-text note. A job with a commit SHA the agent cannot check out, or a command outside the card, returns `blocked` without running.

## Safety rules for the hardware agent

- Run only the commands in the job. Do not craft device commands or byte sequences by hand.
- Brightness `0` is unverified until T129 reports. Run it only when a job asks, and report what the panel did.
- If the panel stops responding, stop all jobs, report `blocked`, and ask a person to power-cycle it.
- Never write to or read from the panel with other tools while a runner is active (one runner per panel).
- Keep secrets out of logs. Soak logs must pass the redaction checks before they are shared.

## Owner-only actions

- Applying branch protection (T133).
- Granting or rotating GitHub credentials, A2A credentials and model keys.
- Physical power-cycles and cabling.
- Changing the constitution or the Hermes guardrail hooks.

## Open items to settle before the first card starts

1. **Confirm** that the VM can reach GitHub and that CI runs on pushes to feature branches and PRs.
2. **Confirm** the A2A message schema matches what the orchestrator already sends; adjust the example above to match.
3. **Confirm** how Hermes maps phases and stories to Kanban cards (the table in `docs/orchestration.md` is a recommendation).
4. **Confirm** that the Reviewer's `/speckit-converge` output is attached to the PR so the owner can audit it.
