#!/usr/bin/env bash
# orch-send / oncall-send: durable inbox append + delivery into the interactive session pane.
# Installed as ~/bin/orch-send and ~/bin/oncall-send (role = basename).
#   orch-send "text" | orch-send --file F | echo text | orch-send - | orch-send --drain
# Exit 0 = saved AND delivered, 2 = saved but not delivered (item is safe in the inbox; the
# session drains it on its next loop), 1 = could not save.
set -uo pipefail
case "$(basename "$0")" in
  orch-send)   ROLE=orchestrator; TAG=orchestrator-main; RUNDIR="$HOME/git/agent-hub-wt-orchestrator-run"; SUB=ops/orchestrator ;;
  oncall-send) ROLE=oncall;       TAG=oncall-engineer;   RUNDIR="$HOME/git/agent-hub-wt-oncall-run"; SUB=ops/oncall ;;
  *) echo "run as orch-send or oncall-send" >&2; exit 64 ;;
esac

INBOX="${OPS_INBOX_DIR:-$HOME/tmp/agent-reports/$ROLE/inbox}"
_runtime_base="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ ! -d "$_runtime_base" ] || [ ! -w "$_runtime_base" ]; then
  _runtime_base="/dev/shm/agent-reports-${USER:-$(id -un)}"
fi
TMPFS_INBOX="${OPS_TMPFS_INBOX_DIR:-$_runtime_base/agent-reports/$ROLE/inbox}"

drain_tmpfs_inbox() {
  [ -d "$TMPFS_INBOX" ] || return 0
  [ "${OPS_SIMULATE_ENOSPC:-0}" = "1" ] && return 0
  mkdir -p "$INBOX/processed" 2>/dev/null || return 0
  local item base tmp_dst dst drained=0
  shopt -s nullglob
  for item in "$TMPFS_INBOX"/*.md; do
    base="$(basename "$item")"
    tmp_dst="$INBOX/.$base.drain.$$"
    dst="$INBOX/$base"
    if cp -- "$item" "$tmp_dst" 2>/dev/null && sync "$tmp_dst" 2>/dev/null && mv -f "$tmp_dst" "$dst" 2>/dev/null; then
      rm -f -- "$item"
      drained=$((drained + 1))
    else
      rm -f -- "$tmp_dst" 2>/dev/null || true
    fi
  done
  shopt -u nullglob
  if [ "$drained" -gt 0 ]; then
    echo "DRAINED=$drained from $TMPFS_INBOX to $INBOX"
  fi
}

if [ "${1:-}" = "--drain" ]; then
  drain_tmpfs_inbox
  exit 0
fi

case "${1:-}" in
  --file) [ -r "${2:-}" ] || { echo "STATUS=error cannot read file" >&2; exit 1; }; body=$(cat "$2") ;;
  -|"")   body=$(cat) ;;
  *)      body="$*" ;;
esac
[ -n "$body" ] || { echo "STATUS=error empty message" >&2; exit 1; }

drain_tmpfs_inbox

id="$(date -u +%Y%m%dT%H%M%S%N)-$$"
write_item() {
  local dir="$1"
  mkdir -p "$dir/processed" 2>/dev/null || return 1
  local tmp="$dir/.$id.tmp" dst="$dir/$id.md"
  if { echo "<!-- id=$id from=${SENDER:-grok-bot} at=$(date -u +%FT%TZ) -->"; printf '%s\n' "$body"; } > "$tmp" 2>/dev/null \
    && sync "$tmp" 2>/dev/null && mv "$tmp" "$dst" 2>/dev/null; then
    f="$dst"
    return 0
  fi
  rm -f -- "$tmp" 2>/dev/null || true
  return 1
}

f=""
if [ "${OPS_SIMULATE_ENOSPC:-0}" != "1" ] && write_item "$INBOX"; then
  echo "STATUS=ok"
  echo "INBOX=saved $f"
elif write_item "$TMPFS_INBOX"; then
  echo "STATUS=fallback_tmpfs"
  echo "INBOX=saved_tmpfs $f"
else
  echo "STATUS=error inbox write failed (primary and tmpfs)" >&2
  exit 1
fi

# Short pane message: full text if small, else a pointer to the file (pane input has size limits).
if [ "${#body}" -le 1500 ]; then
  msg="[INBOX $id] $(printf '%s' "$body" | tr '\n' ' ') (file: $f; when handled, mv it to inbox/processed/)"
else
  msg="[INBOX $id] long message saved at $f - read it, act, then mv it to inbox/processed/"
fi
# An existing managed request always owns resolution, including unknown/error states.
# Never fall back to native friendly-tag selection after a managed failure.
state="$HOME/tmp/agent-reports/$ROLE/state/managed/current.json"
if [ -e "$state" ] || [ -L "$state" ]; then
  ctl="$(cd "$(dirname "$0")" && pwd)/session-ctl.sh"
  # Installed orch-send/oncall-send historically lives in ~/bin; resolve its
  # maintained sibling only when present, never a guessed native session.
  if [ ! -f "$ctl" ]; then
    echo 'DELIVERY=not-delivered (managed controller companion unavailable; inbox retained)'; exit 2
  fi
  if printf '%s' "$msg" | "$ctl" "$ROLE" managed-send "inbox-$id" >/dev/null 2>&1; then
    echo "DELIVERY=delivered to $ROLE via Hub (bound managed identity)"; exit 0
  fi
  echo 'DELIVERY=not-delivered (managed binding or ACK unconfirmed; inbox retained)'; exit 2
fi
sid=$(a list --json 2>/dev/null | python3 -c '
import json,sys
w,t=sys.argv[1:3]
prefix=w.rstrip("/")+"/"
for s in json.load(sys.stdin):
    ws=s.get("workspace") or ""
    if s.get("tag")==t and s.get("phase")=="running" and (ws==w or ws.startswith(prefix)):
        print(s["id"]); break' "$RUNDIR/$SUB" "$TAG")
if [ -z "$sid" ]; then echo "DELIVERY=not-delivered (no running $TAG session; watchdog will restart it, item stays in inbox)"; exit 2; fi
if timeout 60 a send "$sid" "$msg" --enter >/dev/null 2>&1; then echo "DELIVERY=delivered to $TAG ($sid)"; exit 0
else echo "DELIVERY=not-delivered (a send rc=$?; item stays in inbox, drained next loop)"; exit 2; fi
