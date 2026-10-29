#!/usr/bin/env bash
# Hub DB backup staleness alert (intake c1241bea; mail hygiene 9806fc14).
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
# Mail hygiene (intake 9806fc14 — a 2026-10-27 manual verification ran this
# script against empty /tmp dirs with the real env and paged alexey@):
#   1. Test-path guard: when BACKUP_DIR, STORAGEBOX_DIR or STATE_DIR is under
#      /tmp or /var/tmp (test values), EVERY send — including RECOVERED — is
#      routed to ALERT_OPS_TO only, never ALERT_TO. Real recipients require
#      real paths.
#   2. Drill mode: HUB_DB_STALE_DRILL=1 forces one FAIL mail to
#      ALERT_OPS_TO (dev@ only) to prove the mail path; it records a drill
#      marker in the state file with LAST_ALERT=0, so a real staleness
#      episode in the following window is NOT deduped away, and the closing
#      RECOVERED (next fresh run, or the next cron run) also goes to
#      ALERT_OPS_TO only. Re-run without the flag to clear.
#   3. --dry-run / --no-mail / --check-only still send nothing.
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
ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"
ALERT_FROM="${ALERT_FROM:-noreply@datatalks.club}"
ALERT_REPEAT_HOURS="${ALERT_REPEAT_HOURS:-24}"
# msmtp account for the working alerts path on this box. The default msmtp
# account cannot send as ALERT_FROM (SES 554 on the sandbox identity), while
# the 'ses' account demonstrably delivers noreply@datatalks.club to the
# exact ALERT_TO recipients (see msmtp log 250 Ok). Plain msmtp (default
# account) remains the fallback.
MSMTP_ACCOUNT="${MSMTP_ACCOUNT:-ses}"
STATE_DIR="${STATE_DIR:-/var/lib/hub-db-backup}"
DRY_RUN="${DRY_RUN:-0}"
HUB_DB_STALE_DRILL="${HUB_DB_STALE_DRILL:-0}"

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

# Test-path guard (9806fc14): any test-valued dir forces ops-only recipients.
TEST_PATH_GUARD=0
for _guard_path in "$BACKUP_DIR" "$STORAGEBOX_DIR" "$STATE_DIR"; do
  case "$_guard_path" in
    /tmp|/tmp/*|/var/tmp|/var/tmp/*) TEST_PATH_GUARD=1 ;;
  esac
done
unset _guard_path

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
WAS_DRILL=0
if [[ -f "$STATE_FILE" ]]; then
  # Parse numeric/flag fields without sourcing (state dir must not be code).
  LAST_ALERT="$(grep -E '^LAST_ALERT=[0-9]+$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2)"
  WAS_STALE="$(grep -E '^WAS_STALE=[01]$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2)"
  WAS_DRILL="$(grep -E '^DRILL=[01]$' "$STATE_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2)"
  [[ "$LAST_ALERT" =~ ^[0-9]+$ ]] || LAST_ALERT=0
  [[ "$WAS_STALE" =~ ^[01]$ ]] || WAS_STALE=0
  [[ "$WAS_DRILL" =~ ^[01]$ ]] || WAS_DRILL=0
fi

write_state() {
  # $1=WAS_STALE $2=LAST_ALERT $3=DRILL. Never mutate the real state machine
  # on dry-run / no-mail runs (tests must not seed dedupe for prod).
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then return 0; fi
  printf 'WAS_STALE=%s\nLAST_ALERT=%s\nDRILL=%s\n' "$1" "$2" "$3" > "$STATE_FILE" 2>/dev/null || true
}

effective_to() {
  # Real recipients, unless the test-path guard forces ops-only (9806fc14).
  if [[ "$TEST_PATH_GUARD" -eq 1 ]]; then
    printf '%s' "$ALERT_OPS_TO"
  else
    printf '%s' "$ALERT_TO"
  fi
}

send_mail() {
  # $1=subject $2=body [$3=to_csv override — the drill passes ALERT_OPS_TO]
  local subject="$1" body="$2"
  local to_csv="${3:-$(effective_to)}"
  local IFS=','
  # shellcheck disable=SC2206
  local to_args=($to_csv)
  local sent=""
  if command -v sendmail >/dev/null 2>&1; then
    if {
      echo "From: ${ALERT_FROM}"
      echo "To: ${to_csv//,/, }"
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
      echo "To: ${to_csv//,/, }"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | msmtp -a "${MSMTP_ACCOUNT}" -- "${to_args[@]}" 2>/dev/null; then
      sent="msmtp:${MSMTP_ACCOUNT}"
    elif {
      echo "From: ${ALERT_FROM}"
      echo "To: ${to_csv//,/, }"
      echo "Subject: ${subject}"
      echo ""
      printf '%s\n' "$body"
    } | msmtp -- "${to_args[@]}" 2>/dev/null; then
      sent="msmtp:default"
    else
      log "WARN: msmtp failed (account ${MSMTP_ACCOUNT} and default)"
    fi
  fi
  if [[ -z "$sent" ]]; then
    log "WARN: no MTA (sendmail/msmtp) — alert logged only; install msmtp"
  fi
  local guard_note=""
  [[ "$TEST_PATH_GUARD" -eq 1 ]] && guard_note=" guard=test-paths"
  log "ALERT stale=${IS_STALE} subject='${subject}' via=${sent:-none} to=${to_csv}${guard_note}"
}

if [[ "$TEST_PATH_GUARD" -eq 1 ]]; then
  log "GUARD: test paths in BACKUP_DIR/STORAGEBOX_DIR/STATE_DIR — all mail routes to ${ALERT_OPS_TO} only (9806fc14)"
fi

if [[ "$HUB_DB_STALE_DRILL" == "1" ]]; then
  log "DRILL: forcing stale FAIL to ${ALERT_OPS_TO}"
  if [[ "$DRY_RUN" -eq 1 || "$NO_MAIL" -eq 1 ]]; then
    log "would-drill to=${ALERT_OPS_TO} (mail suppressed by flag)"
    exit 2
  fi
  send_mail "[STALE] Hub DB backup drill (ops-only)" \
"DRILL $(date -u '+%Y-%m-%dT%H:%M:%SZ') from $(hostname): forced Hub DB backup staleness drill (HUB_DB_STALE_DRILL=1).
Proves the mail path to ${ALERT_OPS_TO} only — never ALERT_TO.
Run once without the flag on a fresh tree to clear with an ops-only RECOVERED.
A real staleness episode before that is NOT suppressed by this drill." \
  "$ALERT_OPS_TO"
  write_state 1 0 1
  exit 2
fi

if [[ "$IS_STALE" -eq 0 ]]; then
  AGE_H="$(python3 -c "print(f'{$AGE_S/3600:.1f}')" 2>/dev/null || echo '?')"
  log "fresh: newest export age ${AGE_S}s (~${AGE_H}h) path=${NEWEST_PATH}"
  if [[ "${WAS_STALE:-0}" -eq 1 && "$NO_MAIL" -eq 0 && "$DRY_RUN" -eq 0 ]]; then
    if [[ "${WAS_DRILL:-0}" -eq 1 ]]; then
      # Close a drill: RECOVERED stays ops-only (9806fc14), like the deadman's
      # mark_ok_ops — the forced FAIL never pages real recipients backward.
      send_mail "RECOVERED: Hub DB backup drill cleared" \
        "Hub DB backup drill state cleared: newest export is fresh (${NEWEST_PATH}, age ${AGE_S}s)." \
        "$ALERT_OPS_TO"
    else
      send_mail "RECOVERED: Hub DB backup fresh again" \
        "Hub DB backup is fresh again. Newest export: ${NEWEST_PATH} (age ${AGE_S}s)."
    fi
  fi
  write_state 0 "${LAST_ALERT:-0}" 0
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
  local_guard_note=""
  [[ "$TEST_PATH_GUARD" -eq 1 ]] && local_guard_note=" (guard: ops-only)"
  log "would-alert to=$(effective_to)${local_guard_note} (mail suppressed by flag)"
  exit 2
fi

ELAPSED_H=999999
if [[ "$LAST_ALERT" -gt 0 ]]; then
  ELAPSED_H=$(( (NOW - LAST_ALERT) / 3600 ))
fi
if [[ "${WAS_STALE:-0}" -eq 1 && "${WAS_DRILL:-0}" -eq 0 && "$ELAPSED_H" -lt "$ALERT_REPEAT_HOURS" ]]; then
  log "stale but repeat window not reached (${ELAPSED_H}h < ${ALERT_REPEAT_HOURS}h); mail suppressed"
  exit 2
fi
# A pending drill marker never suppresses a real stale page: the drill
# records LAST_ALERT=0, so the window check above already lets it through,
# and this state write drops the DRILL marker.

send_mail "[STALE] Hub DB backup older than ${STALE_AFTER_HOURS}h" \
"Hub DB backup is STALE.
${AGE_DESC}
Local dir: ${BACKUP_DIR}
Storagebox dir: ${STORAGEBOX_DIR}
Run scripts/hub-db-backup.sh and see docs/HUB-DB-BACKUP.md."
write_state 1 "$NOW" 0
exit 2
