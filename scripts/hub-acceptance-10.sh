#!/usr/bin/env bash
# Phase 7 acceptance: ten consecutive model-less Hub starts. See hub_acceptance_10.py.
# --alerts-cmd must supply verified cumulative dev_alert_occurrences_v1 watermarks;
# an active-alert listing cannot prove the no-new-occurrences acceptance criterion.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/hub_acceptance_10.py" "$@"
