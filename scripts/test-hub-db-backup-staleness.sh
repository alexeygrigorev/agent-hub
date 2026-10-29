#!/usr/bin/env bash
# Regression tests for scripts/hub-db-backup-staleness.sh mail hygiene
# (intake 9806fc14). Runs the real script with stub sendmail/msmtp on PATH,
# non-routable *.invalid.test recipients and a fail-closed capture dir —
# the 2026-10-27 incident mailed real recipients from a manual run with
# /tmp dirs; these tests must never be able to repeat that.
#
# Coverage:
#   S1  syntax
#   S2  static mail-hygiene defaults (ALERT_OPS_TO, drill flag, guard)
#   S3  fresh -> exit 0, no mail
#   S4  stale (real-path test dirs) -> exit 2, one mail to ALERT_TO only
#   S5  stale dedupe: repeat inside the window sends nothing
#   S6  --dry-run stale -> would-alert line, zero mail, no state write
#   S7  test-path guard: /tmp dirs route ALL mail to ALERT_OPS_TO, never ALERT_TO
#   S8  drill never mails: FAIL/RECOVERED to the sink only, DRILL=1 state, --drill misuse\n#       refused, symlinked sink fails closed
#   S9  drill must not suppress a real stale page (real recipients, marker dropped)
#   S10 no real recipient ever appears in captured mail; stub MTAs intercept all
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/hub-db-backup-staleness.sh"
INSTALLER="$HERE/install-hub-db-backup.sh"

PASS=0
FAILN=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAILN=$((FAILN+1)); echo "FAIL: $1"; }

T_ALARM="stub-alarm@invalid.test"   # ALERT_TO in every test run
T_OPS="stub-ops@invalid.test"       # ALERT_OPS_TO in every test run

# ---- S1: syntax ----
if bash -n "$SCRIPT" && bash -n "$INSTALLER"; then pass "S1 bash -n (staleness + installer)"; else fail "S1 bash -n"; fi

# ---- S2: static hygiene defaults ----
grep -q 'ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"' "$SCRIPT" \
  && pass "S2a ops recipient default dev-only" || fail "S2a ops recipient default"
grep -q 'HUB_DB_STALE_DRILL="${HUB_DB_STALE_DRILL:-0}"' "$SCRIPT" \
  && pass "S2b drill flag default off" || fail "S2b drill flag default off"
grep -q '/var/tmp|/var/tmp/\*' "$SCRIPT" \
  && pass "S2c test-path guard covers /tmp and /var/tmp" || fail "S2c test-path guard"

# ---- stub MTAs + workroots OUTSIDE /tmp (guard must stay off unless tested) ----
WORK_ROOT="$(mktemp -d "$HOME/.local/state/hub-db-staleness-test.XXXXXX")"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$WORK_ROOT" "$TMP"; }
trap cleanup EXIT
mkdir -p "$TMP/stubbin" "$TMP/capture"
cat > "$TMP/stubbin/sendmail" <<'EOF'
#!/usr/bin/env bash
# Capture mail; fail closed when the capture dir is missing/unwritable.
[[ -d "${STUB_CAP:-}" && -w "$STUB_CAP" ]] || { echo "stub sendmail: no capture dir; refusing" >&2; exit 1; }
{ printf 'SENDMAIL_ARGS\t%s\n' "$*"; cat; printf '\n---MAIL-END---\n'; } >> "$STUB_CAP/mailbox.tsv"
[[ "${STUB_MAIL_FAIL:-0}" == "1" ]] && exit 75
exit 0
EOF
cp "$TMP/stubbin/sendmail" "$TMP/stubbin/msmtp"
sed -i 's/stub sendmail/stub msmtp/' "$TMP/stubbin/msmtp"
sed -i 's/SENDMAIL_ARGS/MSMTP_ARGS/' "$TMP/stubbin/msmtp"
chmod +x "$TMP/stubbin/sendmail" "$TMP/stubbin/msmtp"
# Fail closed: every send path must be stubbed before any test runs.
for s in sendmail msmtp; do
  [[ -x "$TMP/stubbin/$s" ]] || { echo "ABORT: stub '$s' missing" >&2; exit 1; }
done

mailbox="$TMP/capture/mailbox.tsv"
mail_count() { grep -c -- '---MAIL-END---' "$mailbox" 2>/dev/null || true; }

# Non-/tmp fixture dirs: fresh/stale trees + isolated state per case.
new_tree() { # $1=name $2=age_seconds_or_empty -> sets TREE_DIR
  TREE_DIR="$WORK_ROOT/$1"
  rm -rf "$TREE_DIR"; mkdir -p "$TREE_DIR"
  if [[ -n "${2:-}" ]]; then
    f="$TREE_DIR/$(TZ=Europe/Berlin date '+%Y/%m/%d')"
    mkdir -p "$f"
    echo '{"fake":1}' | gzip -c > "$f/hub-do-test.json.gz"
    touch -d "@$(( $(date +%s) - $2 ))" "$f/hub-do-test.json.gz"
  fi
}
new_state() { STATE_DIR="$WORK_ROOT/$1-state"; rm -rf "$STATE_DIR"; mkdir -p "$STATE_DIR"; }

run_stale() { # extra env via STALE_ENV array; uses TREE_DIR/STATE_DIR
  HUB_DB_BACKUP_ENV_FILE=/nonexistent \
  BACKUP_DIR="$TREE_DIR" STORAGEBOX_DIR="$WORK_ROOT/empty-box" \
  STATE_DIR="$STATE_DIR" \
  ALERT_TO="$T_ALARM" ALERT_OPS_TO="$T_OPS" ALERT_FROM="stub-from@invalid.test" \
  MSMTP_ACCOUNT="stub" STUB_CAP="$TMP/capture" \
  PATH="$TMP/stubbin:/usr/bin:/bin" \
  bash "$SCRIPT" "$@" 2>&1
}
mkdir -p "$WORK_ROOT/empty-box"

reset_mail() { : > "$mailbox"; }

# ---- S3: fresh -> exit 0, no mail ----
new_tree fresh 3600; new_state s3
reset_mail
if out="$(run_stale)"; then pass "S3 fresh exits 0"; else fail "S3 fresh exits 0 -- $out"; fi
[[ "$(mail_count)" -eq 0 ]] && pass "S3 fresh sends nothing" || fail "S3 fresh sent mail"

# ---- S4: stale -> exit 2, one mail to ALERT_TO only ----
new_tree old 172800; new_state s4   # 48h old, threshold 36h
reset_mail
set +e; out="$(run_stale)"; rc4=$?; set -e
if [[ $rc4 -eq 2 ]]; then pass "S4 stale exits 2"; else fail "S4 stale rc=$rc4 -- $out"; fi
if [[ "$(mail_count)" -eq 1 ]] && grep -q "To: .*$T_ALARM" "$mailbox" \
   && ! grep -q "$T_OPS" "$mailbox"; then
  pass "S4 stale pages ALERT_TO (single mail, ops not copied)"
else
  fail "S4 stale recipients -- $(cat "$mailbox" 2>/dev/null | head -5)"
fi
grep -q "Subject: \[STALE\] Hub DB backup older than 36h" "$mailbox" \
  && pass "S4 subject carries threshold" || fail "S4 subject"

# ---- S5: dedupe while still stale ----
reset_mail
set +e; out="$(run_stale)"; rc5=$?; set -e
if [[ $rc5 -eq 2 && "$(mail_count)" -eq 0 ]] && grep -q "repeat window not reached" <<<"$out"; then
  pass "S5 repeat stale inside window is suppressed"
else
  fail "S5 dedupe (rc=$rc5 mails=$(mail_count)) -- $out"
fi

# ---- S6: --dry-run sends nothing, writes no state ----
new_tree old 172800; new_state s6
reset_mail
set +e; out="$(run_stale --dry-run)"; rc6=$?; set -e
if [[ $rc6 -eq 2 && "$(mail_count)" -eq 0 ]] && grep -q "would-alert to=$T_ALARM" <<<"$out" \
  && [[ ! -f "$STATE_DIR/staleness.state" ]]; then
  pass "S6 dry-run would-alert, no mail, no state"
else
  fail "S6 dry-run (rc=$rc6 mails=$(mail_count)) -- $out"
fi

# ---- S7: test-path guard routes ALL mail to ops, never ALERT_TO ----
new_tree old 172800
# Forced /tmp paths (the incident pattern) — TMPDIR may point elsewhere, so
# never build these from mktemp.
GUARD_TREE="/tmp/hub-stal-guard-tree.$$"
GUARD_STATE="/tmp/hub-stal-guard-state.$$"
mkdir -p "$GUARD_TREE" "$GUARD_STATE"
reset_mail
set +e
out="$(HUB_DB_BACKUP_ENV_FILE=/nonexistent \
  BACKUP_DIR="$GUARD_TREE" STORAGEBOX_DIR="$WORK_ROOT/empty-box" \
  STATE_DIR="$GUARD_STATE" \
  ALERT_TO="$T_ALARM" ALERT_OPS_TO="$T_OPS" ALERT_FROM="stub-from@invalid.test" \
  MSMTP_ACCOUNT="stub" STUB_CAP="$TMP/capture" \
  PATH="$TMP/stubbin:/usr/bin:/bin" \
  bash "$SCRIPT" 2>&1)"; rc7=$?
set -e
rm -rf "$GUARD_TREE" "$GUARD_STATE"
if [[ $rc7 -eq 2 && "$(mail_count)" -eq 1 ]] \
  && grep -q "To: .*$T_OPS" "$mailbox" && ! grep -q "$T_ALARM" "$mailbox" \
  && grep -q "GUARD: test paths" <<<"$out" && grep -q "guard=test-paths" <<<"$out"; then
  pass "S7 /tmp dirs force ops-only mail (ALERT_TO never used)"
else
  fail "S7 guard (rc=$rc7) -- $out -- $(head -5 "$mailbox" 2>/dev/null)"
fi

# ---- S8: drills are never emailed (standing rule t1940u/e27e0f9d): each
# drill writes exactly one "[DRILL] ... result=... mail=none" log line ----
new_tree fresh 3600; new_state s8
reset_mail
drill_count() { grep -c '\[DRILL\].*result=.*mail=none' <<<"$1" || true; }
set +e; out="$( { export HUB_DB_STALE_DRILL=1; run_stale; } )"; rc8=$?; set -e
if [[ $rc8 -eq 2 && "$(mail_count)" -eq 0 && "$(drill_count "$out")" -eq 1 ]]; then
  pass "S8a drill FAIL: one [DRILL] log line, no mail"
else
  fail "S8a drill FAIL (rc=$rc8, mails=$(mail_count)) -- $out"
fi
grep -q '^DRILL=1$' "$STATE_DIR/staleness.state" && grep -q '^LAST_ALERT=0$' "$STATE_DIR/staleness.state" \
  && pass "S8b drill state: DRILL=1, LAST_ALERT=0" || fail "S8b drill state -- $(cat "$STATE_DIR/staleness.state" 2>/dev/null)"
reset_mail
set +e; out="$(run_stale)"; rc8c=$?; set -e
if [[ $rc8c -eq 0 && "$(mail_count)" -eq 0 && "$(drill_count "$out")" -eq 1 ]] \
  && ! grep -q '^DRILL=1$' "$STATE_DIR/staleness.state"; then
  pass "S8c fresh run clears drill: one [DRILL] line, no mail"
else
  fail "S8c drill recovery (rc=$rc8c, mails=$(mail_count)) -- $out"
fi
# S8d: --drill outside --backup-failed is refused before anything else.
reset_mail
set +e; out="$(run_stale --drill)"; rc8d=$?; set -e
[[ $rc8d -eq 1 && "$(mail_count)" -eq 0 ]] \
  && pass "S8d --drill without --backup-failed refused, no mail" || fail "S8d (rc=$rc8d) -- $out"
# S8e: backup-failed drill: one [DRILL] line, no mail.
reset_mail
set +e; out="$(run_stale --backup-failed "drill reason" --drill)"; rc8e=$?; set -e
[[ $rc8e -eq 0 && "$(mail_count)" -eq 0 && "$(drill_count "$out")" -eq 1 ]] \
  && pass "S8e backup-failed drill: one [DRILL] line, no mail" || fail "S8e (rc=$rc8e) -- $out"
unset HUB_DB_STALE_DRILL

# ---- S9: drill must not suppress a real stale page ----
new_tree old 172800; new_state s9
( export HUB_DB_STALE_DRILL=1; run_stale ) >/dev/null 2>&1 || true
unset HUB_DB_STALE_DRILL
reset_mail
set +e; out="$(run_stale)"; rc9=$?; set -e
if [[ $rc9 -eq 2 && "$(mail_count)" -eq 1 ]] && grep -q "To: .*$T_ALARM" "$mailbox" \
  && grep -q "Subject: \[STALE\] Hub DB backup older than 36h" "$mailbox" \
  && ! grep -q '^DRILL=1$' "$STATE_DIR/staleness.state"; then
  pass "S9 real stale after drill pages ALERT_TO (never muted)"
else
  fail "S9 real stale after drill (rc=$rc9) -- $(head -5 "$mailbox" 2>/dev/null)"
fi

# ---- S9b: legacy state (no DRILL= line) must not kill the run with
# pipefail — this is exactly the pre-9806fc14 prod state file shape; a hard
# exit here would silently disable the staleness cron. ----
new_tree fresh 3600; new_state s9b
printf 'WAS_STALE=0\nLAST_ALERT=1793088901\n' > "$STATE_DIR/staleness.state"
reset_mail
if out="$(run_stale)"; then
  pass "S9b legacy 2-line state file: fresh run exits 0"
else
  fail "S9b legacy state rc=$? -- $out"
fi
[[ "$(mail_count)" -eq 0 ]] && pass "S9b no mail on legacy state" || fail "S9b mailed"
# Corrupt state values also degrade to defaults instead of exiting.
printf 'WAS_STALE=banana\nLAST_ALERT=notanumber\nDRILL=maybe\n' > "$STATE_DIR/staleness.state"
if out="$(run_stale)"; then
  pass "S9c corrupt state values degrade to defaults (exit 0)"
else
  fail "S9c corrupt state rc=$? -- $out"
fi

# ---- S10: hygiene sweep — no real recipient in any captured mail ----
if grep -q "datatalks\.club" "$mailbox" 2>/dev/null; then
  fail "S10 real datatalks.club recipient leaked into captured mail"
else
  pass "S10 no real recipient ever configured in the suite"
fi
if grep -q 'ALERT_TO:-alexey@datatalks' "$SCRIPT" \
  && grep -q 'ALERT_OPS_TO="${ALERT_OPS_TO:-dev@dtcdev.click}"' "$SCRIPT"; then
  pass "S10b prod defaults intact in script (alexey@ real path, ops dev@)"
else
  fail "S10b prod defaults"
fi

echo "---- $PASS passed, $FAILN failed ----"
[[ "$FAILN" -eq 0 ]]
