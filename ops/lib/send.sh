#!/usr/bin/env bash
# orch-send / oncall-send: durable inbox append + delivery into the interactive session pane.
# Installed as ~/bin/orch-send and ~/bin/oncall-send (role = basename).
#   orch-send "text" | orch-send --file F | echo text | orch-send -
# Exit 0 = saved AND delivered, 2 = saved but not delivered (item is safe in the inbox; the
# session drains it on its next loop), 1 = could not save.
set -uo pipefail
case "$(basename "$0")" in
  orch-send)   ROLE=orchestrator; TAG=orchestrator-main; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run" ;;
  oncall-send) ROLE=oncall;       TAG=oncall-engineer;   RUNDIR="$HOME/git/agent-hub-wt-oncall-run" ;;
  *) echo "run as orch-send or oncall-send" >&2; exit 64 ;;
esac
INBOX="$HOME/tmp/agent-reports/$ROLE/inbox"; mkdir -p "$INBOX/processed"
case "${1:-}" in
  --file) [ -r "${2:-}" ] || { echo "STATUS=error cannot read file" >&2; exit 1; }; body=$(cat "$2") ;;
  -|"")   body=$(cat) ;;
  *)      body="$*" ;;
esac
[ -n "$body" ] || { echo "STATUS=error empty message" >&2; exit 1; }
id="$(date -u +%Y%m%dT%H%M%S%N)-$$"
tmp="$INBOX/.$id.tmp"; f="$INBOX/$id.md"
{ echo "<!-- id=$id from=${SENDER:-grok-bot} at=$(date -u +%FT%TZ) -->"; printf '%s\n' "$body"; } > "$tmp" && sync "$tmp" && mv "$tmp" "$f" \
  || { echo "STATUS=error inbox write failed" >&2; exit 1; }
echo "INBOX=saved $f"
# Short pane message: full text if small, else a pointer to the file (pane input has size limits).
if [ "${#body}" -le 1500 ]; then
  msg="[INBOX $id] $(printf '%s' "$body" | tr '\n' ' ') (file: $f; when handled, mv it to inbox/processed/)"
else
  msg="[INBOX $id] long message saved at $f - read it, act, then mv it to inbox/processed/"
fi
sid=$(a list --json 2>/dev/null | python3 -c '
import json,sys
w,t=sys.argv[1:3]
for s in json.load(sys.stdin):
    if s.get("workspace")==w and s.get("tag")==t and s.get("phase")=="running": print(s["id"]); break' "$RUNDIR" "$TAG")
if [ -z "$sid" ]; then echo "DELIVERY=not-delivered (no running $TAG session; liveness cron will restart it, item stays in inbox)"; exit 2; fi
if timeout 60 a send "$sid" "$msg" --enter >/dev/null 2>&1; then echo "DELIVERY=delivered to $TAG ($sid)"; exit 0
else echo "DELIVERY=not-delivered (a send rc=$?; item stays in inbox, drained next loop)"; exit 2; fi
