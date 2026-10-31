#!/usr/bin/env bash
# Uncommitted-fix exit guard (intakes 747d5bdf, 9432731e).
#
# Usage: scripts/require-clean-exit.sh [--intake <id>] [--repo <path>]
#
# Exits 0 when the checkout has no uncommitted changes and either nothing is
# ahead of a freshly fetched origin/main, or the commits ahead sit on the
# session's agent branch (any branch except main/master, not detached) with
# HEAD recorded on the Hub intake as a `commit` or `report` event whose
# reference is the HEAD sha (worker credentials may append only note, report
# and blocker events, so workers record commits as `report`). Otherwise prints the
# state plus the Hub note and followup commands required before stopping,
# and exits 1. If origin/main cannot be fetched or Hub cannot be read, the
# pushed state is unknown and the guard fails closed (exit 1).
#
# A clean result is NOT permission to push to main. Pushing to main happens
# only with the orchestrator-main release slot (AGENTS.md rules 4-5).
#
# HUB_CLI overrides the hub command (tests use a stub).
set -uo pipefail

USAGE="Usage: scripts/require-clean-exit.sh [--intake <id>] [--repo <path>]"
INTAKE=""
REPO=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --intake|--repo)
      if [ "$#" -lt 2 ]; then
        echo "require-clean-exit: $1 needs a value" >&2
        echo "$USAGE" >&2
        exit 2
      fi
      if [ "$1" = "--intake" ]; then INTAKE="$2"; else REPO="$2"; fi
      shift 2 ;;
    -h|--help) echo "$USAGE"; exit 0 ;;
    *) echo "require-clean-exit: unknown option $1" >&2; echo "$USAGE" >&2; exit 2 ;;
  esac
done

if [ -n "$REPO" ]; then
  cd "$REPO" || { echo "require-clean-exit: cannot cd to $REPO" >&2; exit 2; }
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "require-clean-exit: not inside a git work tree" >&2
  exit 2
fi

NO_PUSH="This does NOT authorize pushing to main; push only with the orchestrator-main release slot."

dirty_status="$(git status --porcelain 2>&1)"
dirty_rc=$?
if [ "$dirty_rc" -ne 0 ]; then
  echo "require-clean-exit: git status failed; refusing (effect unknown)" >&2
  exit 1
fi

# A stale or missing origin/main would hide unpushed work: fetch first.
unknown=""
if ! timeout 60 git fetch --quiet origin main >/dev/null 2>&1; then
  unknown="cannot fetch origin main; pushed state unknown"
elif ! git rev-parse --verify --quiet refs/remotes/origin/main >/dev/null; then
  unknown="origin/main is missing after fetch; pushed state unknown"
fi

unpushed=""
if [ -z "$unknown" ]; then
  unpushed="$(git log --oneline refs/remotes/origin/main..HEAD 2>&1)" \
    || unknown="cannot compare HEAD with origin/main; pushed state unknown"
fi

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
head_sha="$(git rev-parse HEAD 2>/dev/null || true)"
recorded=""
if [ -z "$dirty_status" ] && [ -z "$unknown" ] && [ -n "$unpushed" ] && [ -n "$INTAKE" ] \
   && [ -n "$branch" ] && [ "$branch" != "main" ] && [ "$branch" != "master" ]; then
  if intake_json="$(timeout 60 "${HUB_CLI:-hub}" intake show "$INTAKE" 2>/dev/null)"; then
    if printf '%s' "$intake_json" | python3 -c '
import json, sys
head = sys.argv[1].lower()
def events(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from events(value)
    elif isinstance(node, list):
        for value in node:
            yield from events(value)
for event in events(json.load(sys.stdin)):
    ref = event.get("reference")
    if event.get("kind") in ("commit", "report") and isinstance(ref, str):
        ref = ref.strip().lower()
        if len(ref) >= 7 and head.startswith(ref):
            sys.exit(0)
sys.exit(1)' "$head_sha"; then
      recorded="yes"
    fi
  else
    unknown="cannot read Hub intake $INTAKE; recorded state unknown"
  fi
fi

if [ -z "$dirty_status" ] && [ -z "$unknown" ] && [ -z "$unpushed" ]; then
  echo "require-clean-exit: clean (no uncommitted changes, nothing ahead of origin/main). ${NO_PUSH}"
  exit 0
fi
if [ -z "$dirty_status" ] && [ -z "$unknown" ] && [ -n "$recorded" ]; then
  echo "require-clean-exit: clean (committed on agent branch ${branch} at ${head_sha:0:12}, recorded on Hub intake ${INTAKE}). ${NO_PUSH}"
  exit 0
fi

{
  echo "require-clean-exit: HOLD: uncommitted work, unrecorded commits or unknown pushed state; do not exit without a Hub note and a queued follow-up."
  if [ -n "$unknown" ]; then
    echo "--- UNKNOWN: ${unknown} (failing closed) ---"
  fi
  if [ -n "$dirty_status" ]; then
    echo "--- git status --porcelain ---"
    echo "$dirty_status"
  fi
  if [ -n "$unpushed" ]; then
    echo "--- commits ahead of origin/main (not on main, not recorded in Hub) ---"
    echo "$unpushed"
  fi
  echo "--- required before stopping ---"
  id="${INTAKE:-<id>}"
  echo "1) Commit the work on your agent branch (never on main), then record it (workers use kind report; commit needs operator/head credentials):"
  echo "   hub intake show $id  # read current version"
  echo "   echo '{\"kind\":\"report\",\"reference\":\"<full sha>\",\"source\":\"unknown\"}' | hub intake append $id --version <v> --key commit-<unique> --event-file -"
  echo "2) Append a note with the current state and where the fix sits:"
  echo "   echo '{\"kind\":\"note\",\"text\":\"CAPACITY HOLD — <what was done, test output, next step>\"}' | hub intake append $id --version <v> --key hold-<unique> --event-file -"
  echo "3) Queue an automatic follow-up so Hub re-places the work (kind followup; workers, who may append only note/report/blocker, use kind blocker):"
  echo "   echo '{\"kind\":\"followup\",\"text\":\"Retry heavy step after load clears; fix sits at <path/branch>\"}' | hub intake append $id --version <v> --key followup-<unique> --event-file -"
  echo "   (re-read --version between appends; append is CAS-guarded)"
  echo "4) Keep the worktree; do not remove it while the intake is open."
  echo "${NO_PUSH}"
} >&2
exit 1
