#!/usr/bin/env bash
# Original Phase7 AC is default; --formal retains later typed/Hydrogen qualification.
# Ten consecutive model-less starts, actual stdout retained; --attach-output appends its text.
# --alerts-cmd must supply verified cumulative dev_alert_occurrences_v1 watermarks;
# an active-alert listing cannot prove the no-new-occurrences acceptance criterion.
set -euo pipefail
exec python3 "$(dirname "$(readlink -f "$0")")/hub_acceptance_10.py" "$@"
