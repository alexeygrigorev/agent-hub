#!/usr/bin/env bash
# Hub DB backup staleness alert (intake c1241bea).
#
# Newest export older than STALE_AFTER_HOURS (default 36) -> email to ALERT_TO
# (default "alexey@datatalks.club,dev@dtcdev.click") via the working msmtp
# alerts path (sendmail first, msmtp fallback — same order as
# machine-management monitors/coolify-healthcheck.sh).
#
# Checks both trees (Hetzner local BACKUP_DIR and Storagebox STORAGEBOX_DIR);
# the freshest copy across both decides. Missing directory or no exports at
# all counts as stale.
#
# Exit codes: 0 fresh, 2 stale (mail attempted unless --no-mail / DRY_RUN=1),
# 1 on usage/config error. --dry-run prints what would happen without mailing.
# State dir dedupes repeat pages: at most one mail per ALERT_REPEAT_HOURS
# (default 24) while continuously stale; a RECOVERED note goes out once the
# backup is fresh again after a stale episode.
set -euo pipefail

ENV_FILE="${HUB_DB_BACKUP_ENV_FILE:-/etc/hub-db-backup.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

BACKUP_DIR="${BACKUP_DIR:-/var/backups/hub-do}"
STORAGEBOX_DIR="${STORAGEBOX_DIR:-/home/alexey/storagebox/backups/hub-do}"
STALE_AFTER_HOURS="${STALE_AFTER_HOURS:-36}"
ALERT_TO="${ALERT_TO:-alexey@datatalks.club,dev@dtcdev.click}"
ALERT_FROM="${ALERT_FROM:-noreply@datatalks.club}"
ALERT_REPEAT_HOURS="${ALERT_REPEAT_HOURS:-24}"
STATE_DIR="${STATE_DIR:-/var/lib/hub-db-backup}"
DRY_RUN="${DRY_RUN:-0}"

NO_MAIL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-mail) NO_MAIL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --check-only) NO_MAIL=1; DRY_RUN=1; shift ;;
    -h|--help)
      echo "Usage: hub-db-backup-staleness.sh [--no-mail] [--dry-run] [--check-only]"
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ "$STALE_AFTER_HOURS" =~ ^[0-9]+$ ]] || { log "ERROR: STALE_AFTER_HOURS must be an integer"; exit 1; }
[[ "$ALERT_REPEAT_HOURS" =~ ^[0-9]+$ ]] || { log "ERROR: ALERT_REPEAT_HOURS must be an integer"; exit 1; }

newest_epoch() {
  local root="$1" newest=""
  if [[ -d "$root" ]]; then
    newest="$(find "$root" -type f -name 'hub-do-*.json.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 || true)"
  fi
  if [[ -n "$newest" ]]; then
    printf '%s' "${newest%% *}" | cut -d. -f1
  fi
}

NOW="$(date +%s)"
LOCAL_NEWEST="$(newest_epoch "$BACKUP_DIR")"
BOX_NEWEST="$(newest_epoch "$STORAGEBOX_DIR")"
NEWEST=0
NEWEST_PATH=""
if [[ -n "$LOCAL_NEWEST" && "$LOCAL_NEWEST" -gt "$NEWEST" ]]; then
  NEWEST="$LOCAL_NEWEST"
  NEWEST_PATH="$(find "$BACKUP_DIR" -type f -name 'hub-do-*.json.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
fi
if [[ -n "$BOX_NEWEST" && "$BOX_NEWEST" -gt "$NEWEST" ]]; then
  NEWEST="$BOX_NEWEST"
  NEWEST_PATH="$(find "$STORAGEBOX_DIR" -type f -name 'hub-do-*.json.gz' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -n 1 | cut -d' ' -f2-)"
fi

if [[ "$NEWEST" -le 0 ]]; then
  AGE_S=-1
else
  AGE_S=$(( NOW - NEWEST ))
fi
STALE_AFTER_S=$(( STALE_AFTER_HOURS * 3600 ))
IS_STALE=1
if [[ "$AGE_S" -ge 0 && "$AGE_S" -le "$STALE_AFTER_S" ]]; then
  IS_STALE=0
fi

mkdir -p "$STATE_DIR" 2>/dev/null || true
chmod 700 "$STATE_DIR" 2>/dev/null || true
STATE_FILE="${STATE_DIR%/}/staleness.state"
LAST_ALERT=0
WAS_STALE=0
if [[ -f "$STATE_FILE" ]]; then
  # Parse two numeric fields without sourcing (state dir must not be code).
  LAST_ALERT="$(grep -E '^LAST_ALERT=[0-9]+$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2)"
  WAS_STALE="$(grep -E '^WAS_STALE=[01]$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2)"
  [[ "$LAST_ALERT" =~ ^[0-9]+$ ]] || LAST_ALERT=0
  [[ "$WAS_STALE" =~ ^[01]$ ]] || WAS_STALE=0
fi

write_state() {
  # Never mutate the real state machine on dry runs.
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then return 0; fi
  printf 'WAS_STALE=%s\nLAST_ALERT=%s\n' "$1" "$2" > "$STATE_FILE" 2>/dev/null || true
}

send_mail() {
  local subject="$1" body="$2"
  local IFS=','
  # shellcheck disable=SC2206
  local to_args=($ALERT_TO)
  local sent=""
  if command -v sendmail >/dev/null 2>&1; then
    if {
      echo "From: ${ALERT_FROM}"
      echo "To: ${ALERT_TO}"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | sendmail -t 2>/dev/null; then
      sent="sendmail"
    else
      log "WARN: sendmail failed"
    fi
  fi
  if [[ -z "$sent" ]] && command -v msmtp >/dev/null 2>&1; then
    if {
      echo "From: ${ALERT_FROM}"
      echo "To: ${ALERT_TO}"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | msmtp -- "${to_args[@]}" 2>/dev/null; then
      sent="msmtp"
    else
      log "WARN: msmtp failed"
    fi
  fi
  if [[ -z "$sent" ]]; then
    log "WARN: no MTA (sendmail/msmtp) — alert logged only; install msmtp"
  fi
  log "ALERT stale=${IS_STALE} subject='${subject}' via=${sent:-none} to=${ALERT_TO}"
}

if [[ "$IS_STALE" -eq 0 ]]; then
  AGE_H="$(python3 -c "print(f'{$AGE_S/3600:.1f}')" 2>/dev/null || echo '?')"
  log "fresh: newest export age ${AGE_S}s (~${AGE_H}h) path=${NEWEST_PATH}"
  if [[ "${WAS_STALE:-0}" -eq 1 && "$NO_MAIL" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    send_mail "RECOVERED: Hub DB backup fresh again" \
      "Hub DB backup is fresh again. Newest export: ${NEWEST_PATH} (age ${AGE_S}s)."
  fi
  write_state 0 "${LAST_ALERT:-0}"
  exit 0
fi

# Stale branch.
if [[ "$AGE_S" -lt 0 ]]; then
  AGE_DESC="no exports found"
else
  AGE_DESC="newest export age ${AGE_S}s (~$(( AGE_S / 3600 ))h), threshold ${STALE_AFTER_HOURS}h path=${NEWEST_PATH:-none}"
fi
log "STALE: ${AGE_DESC}"

if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then
  log "would-alert to=${ALERT_TO} (mail suppressed by flag)"
  exit 2
fi

ELAPSED_H=999999
if [[ "$LAST_ALERT" -gt 0 ]]; then
  ELAPSED_H=$(( (NOW - LAST_ALERT) / 3600 ))
fi
if [[ "${WAS_STALE:-0}" -eq 1 && "$ELAPSED_H" -lt "$ALERT_REPEAT_HOURS" ]]; then
  log "stale but repeat window not reached (${ELAPSED_H}h < ${ALERT_REPEAT_HOURS}h); mail suppressed"
  exit 2
fi

send_mail "[STALE] Hub DB backup older than ${STALE_AFTER_HOURS}h" \
"Hub DB backup is STALE.
${AGE_DESC}
Local dir: ${BACKUP_DIR}
Storagebox dir: ${STORAGEBOX_DIR}
Run scripts/hub-db-backup.sh and see docs/HUB-DB-BACKUP.md."
write_state 1 "$NOW"
exit 2
