#!/usr/bin/env bash
# Idempotent installer for the Hub start canary timers (system units).
#   sudo bash scripts/install-hub-start-canary.sh [--user NAME]   install + enable timers
#   bash scripts/install-hub-start-canary.sh --verify              render units to a temp dir and run systemd-analyze verify (no install)
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
VERIFY=0
while (($#)); do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --verify) VERIFY=1; shift ;;
    *) echo "usage: install-hub-start-canary.sh [--user NAME] [--verify]" >&2; exit 2 ;;
  esac
done
RUN_GROUP="$(id -gn "$RUN_USER")"
LIB=/usr/local/sbin/hub-start-canary
UNITS=(hub-start-canary.service hub-start-canary.timer hub-start-canary-full.service hub-start-canary-full.timer)

render() { sed -e "s/@CANARY_USER@/$RUN_USER/" -e "s/@CANARY_GROUP@/$RUN_GROUP/" "$REPO_ROOT/deploy/$1"; }

if (( VERIFY )); then
  command -v systemd-analyze >/dev/null || { echo "systemd-analyze not available" >&2; exit 2; }
  VERIFY_BASE="${TMPDIR:-$REPO_ROOT/.local/tmp}"
  mkdir -p "$VERIFY_BASE"
  TMP="$(mktemp -d "$VERIFY_BASE/hub-canary-verify.XXXXXX")"
  trap 'rm -rf -- "$TMP"' EXIT
  mkdir -p "$TMP/lib"
  # Rewrite ExecStart to a real file so verify has something to resolve.
  install -m 755 "$REPO_ROOT/scripts/hub-start-canary.sh" "$TMP/lib/hub-start-canary.sh"
  for u in "${UNITS[@]}"; do render "$u" | sed "s#$LIB/#$TMP/lib/#" > "$TMP/$u"; done
  systemd-analyze verify "$TMP"/hub-start-canary.service "$TMP"/hub-start-canary.timer \
    "$TMP"/hub-start-canary-full.service "$TMP"/hub-start-canary-full.timer
  echo "verify ok"
  exit 0
fi

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "run as root: sudo bash scripts/install-hub-start-canary.sh" >&2; exit 2; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "user $RUN_USER does not exist" >&2; exit 2; }
install -d -o root -g root -m 755 "$LIB"
install -o root -g root -m 755 "$REPO_ROOT/scripts/hub-start-canary.sh" "$LIB/hub-start-canary.sh"
install -o root -g root -m 755 "$REPO_ROOT/scripts/hub_start_canary.py" "$LIB/hub_start_canary.py"
for u in "${UNITS[@]}"; do
  render "$u" | install -o root -g root -m 644 /dev/stdin "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now hub-start-canary.timer hub-start-canary-full.timer
echo "Hub start canary timers installed for user $RUN_USER (shell every 5 min, full every 30 min)."
