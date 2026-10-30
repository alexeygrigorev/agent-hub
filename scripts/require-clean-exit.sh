#!/usr/bin/env bash
# Uncommitted-fix exit guard (intake 747d5bdf AC2).
#
# Usage: scripts/require-clean-exit.sh [--intake <id>] [--repo <path>]
#
# Exits 0 when the checkout has no uncommitted changes and no unpushed
# commits. Otherwise prints the dirty state plus the exact Hub note and
# followup commands required before stopping, and exits 1.
#
# A session must never exit with an uncommitted fix without a Hub note and
# an automatically queued follow-up. The follow-up is a `followup` event on
# the intake, which keeps it in the unresolved set for Hub's forgotten-work
# projection and restart queue. Keep the worktree while blocked or waiting.
set -uo pipefail

INTAKE=""
REPO=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --intake) INTAKE="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    -h|--help) echo "Usage: scripts/require-clean-exit.sh [--intake <id>] [--repo <path>]"; exit 0 ;;
    *) echo "require-clean-exit: unknown option $1" >&2; exit 2 ;;
  esac
done

if [ -n "$REPO" ]; then
  cd "$REPO" || { echo "require-clean-exit: cannot cd to $REPO" >&2; exit 2; }
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "require-clean-exit: not inside a git work tree" >&2
  exit 2
fi

dirty_status="$(git status --porcelain 2>&1)"
dirty_rc=$?
if [ "$dirty_rc" -ne 0 ]; then
  echo "require-clean-exit: git status failed; refusing (effect unknown)" >&2
  exit 1
fi

unpushed=""
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  unpushed="$(git log --oneline origin/main..HEAD 2>/dev/null || true)"
fi

if [ -z "$dirty_status" ] && [ -z "$unpushed" ]; then
  echo "require-clean-exit: clean (no uncommitted changes, nothing unpushed)"
  exit 0
fi

{
  echo "require-clean-exit: HOLD: uncommitted or unpushed fix present; do not exit without a Hub note and a queued follow-up."
  if [ -n "$dirty_status" ]; then
    echo "--- git status --porcelain ---"
    echo "$dirty_status"
  fi
  if [ -n "$unpushed" ]; then
    echo "--- unpushed commits (origin/main..HEAD) ---"
    echo "$unpushed"
  fi
  echo "--- required before stopping ---"
  if [ -n "$INTAKE" ]; then
    echo "1) Append a note with the current state and where the fix sits:"
    echo "   hub intake show $INTAKE"
    echo "   echo '{\"kind\":\"note\",\"text\":\"CAPACITY HOLD — <what was done, test output, next step>\"}' | hub intake append $INTAKE --version <v> --key hold-\$(date +%s) --event-file -"
    echo "2) Queue an automatic follow-up so Hub re-places the work:"
    echo "   echo '{\"kind\":\"followup\",\"text\":\"Retry heavy step after load clears; fix sits at <path/branch>\"}' | hub intake append $INTAKE --version <v> --key followup-\$(date +%s) --event-file -"
  else
    echo "1) hub intake show <id>  # read current version"
    echo "2) echo '{\"kind\":\"note\",\"text\":\"CAPACITY HOLD — <state, test output, next step>\"}' | hub intake append <id> --version <v> --key hold-<unique> --event-file -"
    echo "3) echo '{\"kind\":\"followup\",\"text\":\"Retry heavy step after load clears; fix sits at <path/branch>\"}' | hub intake append <id> --version <v> --key followup-<unique> --event-file -"
    echo "   (re-check --version between the two appends; append is CAS-guarded)"
  fi
  echo "4) Keep the worktree; do not remove it while the intake is open."
} >&2
exit 1
