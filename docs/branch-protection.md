# Branch protection on `main`

The constitution's Contribution Workflow requires `main` to be protected: a pull
request before merge, the Phase-0 CI checks passing, force pushes and branch
deletion disabled, and the rules applying to administrators too. This document
records the settings and how to verify them. **The settings themselves live only on
GitHub**, so this doc plus `scripts/protect_main.sh` are the reviewable record.

## Apply

```bash
./scripts/protect_main.sh --dry-run   # show the exact payload, change nothing
./scripts/protect_main.sh             # apply (requires admin)
./scripts/protect_main.sh --verify    # read the live settings back
```

Applying is an outward-facing change to the remote repository, so the owner runs it
deliberately (task T133).

## What it sets, and why

| Setting | Value | Why |
|---|---|---|
| `required_status_checks.strict` | `true` | Branch must be up to date with `main` before merging — catches the "independently green, red together" case. |
| `required_status_checks.contexts` | the four CI job names | The exact contexts CI reports, matrix values included. |
| `enforce_admins` | `true` | The rules apply to administrators, as the constitution requires. |
| `required_approving_review_count` | **0** | See below — a 1 here deadlocks the repo. |
| `allow_force_pushes` | `false` | History on `main` is append-only. |
| `allow_deletions` | `false` | `main` cannot be deleted. |
| `restrictions` | `null` | No push allowlist; protection is by PR + CI, not by account. |

### Why zero required approvals

This repository has **a single maintainer account** (`hortocam`). GitHub refuses to
let an account approve its own pull request (`Can not approve your own pull
request`), and every PR here is opened by that account, so
`required_approving_review_count: 1` is **unsatisfiable** — it would block every
merge, for the agent and for the human alike.

The review control therefore lives on the **process**, not on this setting: an
independent reviewer (a different model lineage) reviews and opens the PR, and the
merge authority merges. When a second account exists, raise this to `1` and set
`enforce_admins: true` — first proving the loop with a real PR merged **without**
`--admin`, since an admin merge bypasses the very rule under test.

### The required check names must match CI exactly

A required context that matches no job **silently blocks every merge**: the PR sits
`BLOCKED` with all checks green and the *missing* context as the cause. Read the
names off a real run rather than transcribing them:

```bash
gh api repos/hortocam/smart-panel/commits/main/check-runs --jq '.check_runs[].name' | sort -u
```

The CI matrix (`.github/workflows/ci.yml`) produces exactly:

```
test (macos-latest, 3.11)
test (macos-latest, 3.13)
test (ubuntu-latest, 3.11)
test (ubuntu-latest, 3.13)
```

If the workflow matrix changes, update `REQUIRED_CHECKS` in `scripts/protect_main.sh`
and re-apply — a stale required context is worse than none.

## Verify

```bash
./scripts/protect_main.sh --verify
```

or directly:

```bash
gh api repos/hortocam/smart-panel/branches/main/protection
gh api repos/hortocam/smart-panel/branches/main/protection/enforce_admins --jq .enabled
```

**Verify the effect, not just the setting.** A protection flag reading `true` is not
evidence it bites. The negative control is a throwaway commit attempted as a direct
push to `main`, which must be refused with `GH006: Protected branch update failed …
Changes must be made through a pull request.` Note that with `enforce_admins` on a
single-account repo, the bypass a maintainer previously had via `--admin` is closed,
so the merge path becomes ordinary PR merges.
