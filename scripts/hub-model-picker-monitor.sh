#!/usr/bin/env bash
# Mails dev@dtcdev.click (never orin@) when the Hub model picker has to exclude a
# candidate because its provider credential is expired/unusable. An "expires in N
# days" warning is not an exclusion: the picker only drops unusable credentials.
# Reads the dry-run endpoint (nothing is started); never prints token values.
set -euo pipefail
BASE_URL="${HUB_BASE_URL:-https://hub.dtcdev.click}"
CLI_JSON="${HUB_CLI_JSON:-$HOME/.config/agent-hub/cli.json}"
STATE="${HUB_PICKER_MONITOR_STATE:-$HOME/.local/state/hub-model-picker-monitor/last}"
REPEAT_SECONDS="${HUB_PICKER_MONITOR_REPEAT_SECONDS:-21600}"
ALERT_TO="dev@dtcdev.click"
ALERT_CMD="${HUB_PICKER_ALERT_CMD:-$HOME/.local/bin/principal-alert.sh}"
delivery_status=0
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$script_dir/hub-picker-alerts.py" --base-url "$BASE_URL" --cli-json "$CLI_JSON" --helper "$ALERT_CMD" || delivery_status=$?
mkdir -p "$(dirname "$STATE")"
token="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' "$CLI_JSON")"
excluded="$(curl -fsS --max-time 30 -X POST -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{}' "$BASE_URL/api/model-picker/pick" \
  | python3 -I -c 'import json,sys; print(",".join(sorted("%s=%s" % (r["policy_id"], r["reason"]) for r in json.load(sys.stdin).get("excluded", []))))')"
now="$(date +%s)"
if [[ -z "$excluded" ]]; then rm -f -- "$STATE"; echo "ok: no candidate excluded"; exit "$delivery_status"; fi
if [[ -f "$STATE" ]]; then
  read -r last_at last_list < "$STATE" || true
  if [[ "${last_list:-}" == "$excluded" && $(( now - ${last_at:-0} )) -lt $REPEAT_SECONDS ]]; then echo "alert already sent: $excluded"; exit "$delivery_status"; fi
fi
msg="Hub model picker excluded candidates with unusable credentials: $excluded. Re-login or sync the provider credential."
PRINCIPAL_ALERT_TO="$ALERT_TO" PING_ALERT_MESSAGE="$msg" "$ALERT_CMD" "$msg"
printf '%s %s\n' "$now" "$excluded" > "$STATE"
echo "alerted $ALERT_TO: $excluded"

exit "$delivery_status"
