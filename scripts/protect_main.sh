#!/usr/bin/env bash
# Apply branch protection to `main` for hortocam/smart-panel.
#
# Constitution, Contribution Workflow: a pull request is required before merge,
# the Phase-0 CI checks must pass, force pushes and branch deletion are disabled,
# and the rules apply to administrators too. This script records those settings so
# they can be reviewed and re-applied (the settings themselves are not versioned
# by GitHub, so this file IS the record).
#
# Requires admin on the repository. Run it as the repository owner:
#
#     ./scripts/protect_main.sh                 # apply
#     ./scripts/protect_main.sh --dry-run       # show the payload, change nothing
#     ./scripts/protect_main.sh --verify        # read the live settings back
#
# Applying it is an outward-facing change to the remote repository: the owner runs
# it deliberately (task T133).

set -euo pipefail

REPO="${REPO:-hortocam/smart-panel}"
BRANCH="${BRANCH:-main}"

# The required status-check contexts MUST be the exact job names CI reports,
# matrix values included. A required check name that matches no job silently
# blocks every merge (the PR sits BLOCKED with the *missing* context, not a
# failing one). Read them off a real run:
#   gh api repos/$REPO/commits/$BRANCH/check-runs --jq '.check_runs[].name'
REQUIRED_CHECKS=(
  "test (ubuntu-latest, 3.11)"
  "test (ubuntu-latest, 3.13)"
  "test (macos-latest, 3.11)"
  "test (macos-latest, 3.13)"
)

MODE="${1:-apply}"

payload() {
  # required_approving_review_count is 0: the project has a single maintainer, and
  # GitHub refuses to let an account approve its own pull request, so requiring 1
  # approval would deadlock every merge. See docs/branch-protection.md.
  python3 - "$@" <<'PY'
import json, os
checks = os.environ["CHECKS"].split("\n")
print(json.dumps({
    "required_status_checks": {"strict": True, "contexts": checks},
    "enforce_admins": True,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": False,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 0,
    },
    "restrictions": None,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "required_conversation_resolution": False,
}))
PY
}

export CHECKS="$(printf '%s\n' "${REQUIRED_CHECKS[@]}")"

case "$MODE" in
  --dry-run)
    echo "Would PUT to repos/$REPO/branches/$BRANCH/protection:"
    payload | python3 -m json.tool
    ;;

  --verify)
    echo "Live protection for $REPO@$BRANCH:"
    gh api "repos/$REPO/branches/$BRANCH/protection" || {
      echo "!! Branch is NOT protected (404)" >&2; exit 1; }
    echo
    echo "Required checks:"
    gh api "repos/$REPO/branches/$BRANCH/protection" \
      --jq '.required_status_checks.contexts[]' | sed 's/^/  - /'
    echo
    echo "enforce_admins: $(gh api "repos/$REPO/branches/$BRANCH/protection/enforce_admins" --jq .enabled)"
    ;;

  apply)
    echo "Applying protection to $REPO@$BRANCH ..."
    payload | gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" --input - >/dev/null
    echo "Done. Verifying:"
    exec "$0" --verify
    ;;

  *)
    echo "usage: $0 [--dry-run|--verify]" >&2
    exit 2
    ;;
esac
