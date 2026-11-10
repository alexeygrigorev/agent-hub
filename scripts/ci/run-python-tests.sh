#!/usr/bin/env bash
# Run a pytest target under a disposable HOME with a tracked quarantine list.
#   scripts/ci/run-python-tests.sh <quarantine.md> <pythonpath> -- <pytest args...>
# Quarantine file: every line of the form "- `<pytest node id or file>` - reason"
# becomes a --deselect. Deselected tests are printed loudly so the debt stays visible.
set -euo pipefail
quarantine=$1; pythonpath=$2; shift 2
[ "${1:-}" = "--" ] && shift
deselect=()
if [ -f "$quarantine" ]; then
  while IFS= read -r id; do
    [ -n "$id" ] && deselect+=(--deselect "$id")
  done < <(sed -n 's/^- `\([^`]*\)`.*/\1/p' "$quarantine")
fi
echo "::notice::quarantined tests deselected from $quarantine: $(( ${#deselect[@]} / 2 ))"
[ ${#deselect[@]} -gt 0 ] && printf 'QUARANTINED: %s\n' "${deselect[@]}" | grep -v -- '^QUARANTINED: --deselect$' || true
repo=$(pwd)
exec "$repo/scripts/with-temp-home.sh" -- env PYTHONPATH="$pythonpath" \
  python -m pytest -q -p no:cacheprovider --basetemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/pytest-basetemp" \
  "${deselect[@]}" "$@"
