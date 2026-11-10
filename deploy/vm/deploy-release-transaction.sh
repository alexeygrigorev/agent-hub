#!/usr/bin/env bash
# Transactional Hub2 VM deployment, streamed from a reviewed GitHub Actions job.
# This script deliberately accepts only fixed operations and a commit SHA; it
# never accepts an arbitrary remote command.
set -Eeuo pipefail

if [[ "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" == 1 ]]; then
  REPO="${AGENT_HUB_TEST_REPO:?AGENT_HUB_TEST_REPO is required in test mode}"
  TRANSACTION_DIR="${AGENT_HUB_TEST_TRANSACTION_DIR:?AGENT_HUB_TEST_TRANSACTION_DIR is required in test mode}"
  CADDY_CONFIG="${AGENT_HUB_TEST_CADDY_CONFIG:?AGENT_HUB_TEST_CADDY_CONFIG is required in test mode}"
  CADDY_SITE="${AGENT_HUB_TEST_CADDY_SITE:-$(dirname "$CADDY_CONFIG")/sites/hub.caddy}"
else
  REPO=/opt/agent-hub
  TRANSACTION_DIR=/var/lib/agent-hub/deploy-rollback
  CADDY_CONFIG=/etc/caddy/Caddyfile
  CADDY_SITE=/etc/caddy/sites/hub.caddy
fi
CADDY_BIN=caddy
TMPDIR="$REPO/.local/tmp"
CANDIDATE_CADDY=""

cleanup_candidate() {
  if [[ -n "$CANDIDATE_CADDY" ]]; then
    rm -f -- "$CANDIDATE_CADDY"
  fi
}
trap cleanup_candidate EXIT

run_root() {
  if [[ "$(id -u)" == 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

validate_hub_caddyfile() {
  local config="$1"
  [[ -s "$config" ]] || return 1
  grep -Fq 'reverse_proxy 127.0.0.1:8787' "$config" || return 1
  run_root "$CADDY_BIN" validate --config "$config" --adapter caddyfile || return 1
}

validate_shared_caddyfile() {
  [[ -s "$CADDY_CONFIG" ]] || return 1
  grep -Eq '^[[:space:]]*import[[:space:]]+/etc/caddy/sites/\*\.caddy[[:space:]]*$' "$CADDY_CONFIG" || return 1
  run_root "$CADDY_BIN" validate --config "$CADDY_CONFIG" --adapter caddyfile || return 1
}

write_root_file() {
  local path="$1" contents="$2"
  printf '%s\n' "$contents" | run_root tee "$path" >/dev/null
  run_root chmod 600 "$path"
}

read_root_file() {
  run_root cat "$1"
}

wait_for_hub_health() {
  local attempt
  for ((attempt = 1; attempt <= 15; attempt++)); do
    if curl -fsS --max-time 3 http://127.0.0.1:8787/health >/dev/null 2>&1; then
      printf 'Hub health check passed on attempt %s/15.\n' "$attempt"
      return 0
    fi
    if ((attempt < 15)); then sleep 1; fi
  done
  printf 'ERROR: Hub API did not pass its local health check after 15 attempts.\n' >&2
  return 1
}

install_release_dependencies() {
  TMPDIR="$TMPDIR" npm --prefix "$REPO" ci --omit=dev
}

remove_transaction_state() {
  run_root rm -rf -- "$TRANSACTION_DIR"
}

restore_previous_release() {
  local previous_sha vars_present caddy_mode site_present
  if ! run_root test -f "$TRANSACTION_DIR/prepared"; then
    printf 'No pending deploy transaction; nothing to roll back.\n'
    return 0
  fi
  cd "$REPO" || return 1

  previous_sha="$(read_root_file "$TRANSACTION_DIR/previous.sha")"
  [[ "$previous_sha" =~ ^[0-9a-f]{40}$ ]] \
    || { printf 'ERROR: rollback metadata has an invalid previous commit.\n' >&2; return 1; }
  git -C "$REPO" cat-file -e "${previous_sha}^{commit}" \
    || { printf 'ERROR: previous release commit is unavailable locally: %s\n' "$previous_sha" >&2; return 1; }

  printf 'Rolling Hub2 back to %s\n' "$previous_sha"
  git -C "$REPO" checkout --detach --force "$previous_sha" || return 1
  install_release_dependencies || return 1

  caddy_mode=full
  if run_root test -f "$TRANSACTION_DIR/caddy-mode"; then
    caddy_mode="$(read_root_file "$TRANSACTION_DIR/caddy-mode")"
  fi
  case "$caddy_mode" in
    full) ;;
    hub-only) ;;
    *) printf 'ERROR: rollback metadata has an invalid caddy mode.\n' >&2; return 1 ;;
  esac

  vars_present="$(read_root_file "$TRANSACTION_DIR/dev-vars-present")"
  case "$vars_present" in
    yes)
      run_root install -o alexey -g alexey -m 600 \
        "$TRANSACTION_DIR/dev.vars.previous" "$REPO/.dev.vars" || return 1
      ;;
    no)
      run_root rm -f -- "$REPO/.dev.vars" || return 1
      ;;
    *) printf 'ERROR: rollback metadata for .dev.vars is invalid.\n' >&2; return 1 ;;
  esac

  # Always suppress edge writes from old installers, which may own Caddyfile.
  run_root bash "$REPO/deploy/vm/install-hub2-node.sh" --hub-only || return 1

  if [[ "$caddy_mode" == full ]]; then
    site_present="$(read_root_file "$TRANSACTION_DIR/hub-site-present")"
    case "$site_present" in
      yes)
        run_root install -o root -g root -m 644 \
          "$TRANSACTION_DIR/hub.caddy.previous" "$CADDY_SITE" || return 1
        ;;
      no) run_root rm -f -- "$CADDY_SITE" || return 1 ;;
      *) printf 'ERROR: invalid Hub site rollback metadata.\n' >&2; return 1 ;;
    esac
    validate_shared_caddyfile || return 1
    run_root systemctl reload caddy || return 1
  fi
  run_root systemctl restart agent-hub-hub2.service || return 1
  run_root systemctl is-active --quiet agent-hub-hub2.service || return 1
  wait_for_hub_health || return 1

  remove_transaction_state || return 1
  if [[ "$caddy_mode" == full ]]; then
    printf 'Rollback completed: previous Hub2 release, .dev.vars, and Hub site restored; shared Caddyfile untouched.\n'
  else
    printf 'Rollback completed: previous Hub2 release and .dev.vars restored; Caddy was left untouched.\n'
  fi
}

apply_release() {
  local deploy_sha="$1" release_mode="$2" caddy_mode="$3" previous_sha local_status
  [[ "$deploy_sha" =~ ^[0-9a-f]{40}$ ]] || fail "deploy SHA must be a full lowercase Git commit SHA"
  [[ "$release_mode" == bootstrap || "$release_mode" == active ]] \
    || fail "release mode must be bootstrap or active"
  [[ "$caddy_mode" == full || "$caddy_mode" == hub-only ]] \
    || fail "caddy mode must be full or hub-only"
  [[ -d "$REPO/.git" ]] || fail "Hub repository checkout is missing: $REPO"
  cd "$REPO" || fail "cannot enter Hub repository checkout: $REPO"
  [[ -f "$REPO/.dev.vars" ]] \
    || fail "refusing an unrollbackable deploy because the existing Hub release vars are missing"

  run_root test ! -e "$TRANSACTION_DIR" \
    || fail "an earlier deploy transaction is still pending; resolve its rollback first"
  git -C "$REPO" fetch --quiet --no-tags origin main
  git -C "$REPO" cat-file -e "${deploy_sha}^{commit}" \
    || fail "requested deploy commit is unavailable: $deploy_sha"
  git -C "$REPO" merge-base --is-ancestor "$deploy_sha" "$(git -C "$REPO" rev-parse origin/main)" \
    || fail "requested deploy commit is not reachable from origin/main: $deploy_sha"
  local_status="$(git -C "$REPO" status --porcelain --untracked-files=all)"
  [[ -z "$local_status" ]] \
    || fail "refusing deployment because the VM checkout has uncommitted files"

  mkdir -p "$TMPDIR"
  chmod 750 "$TMPDIR"
  if [[ "$caddy_mode" == full ]]; then
    CANDIDATE_CADDY="$(mktemp "$TMPDIR/hub-caddy-site.XXXXXX")"
    git -C "$REPO" show "${deploy_sha}:deploy/hub.caddy" > "$CANDIDATE_CADDY" \
      || fail "requested revision has no deploy/hub.caddy"
    validate_hub_caddyfile "$CANDIDATE_CADDY" \
      || fail "candidate Hub site must validate and retain the Hub upstream"
  fi

  previous_sha="$(git -C "$REPO" rev-parse HEAD)"
  [[ "$previous_sha" =~ ^[0-9a-f]{40}$ ]] || fail "current VM revision is not a full commit SHA"
  if [[ "$caddy_mode" == full ]]; then
    validate_shared_caddyfile || fail "shared Caddyfile must import /etc/caddy/sites/*.caddy; aws-infra owns that migration"
  fi

  run_root install -d -o root -g root -m 700 "$TRANSACTION_DIR"
  if [[ "$caddy_mode" == full ]]; then
    if run_root test -f "$CADDY_SITE"; then
      run_root cp -p -- "$CADDY_SITE" "$TRANSACTION_DIR/hub.caddy.previous"
      write_root_file "$TRANSACTION_DIR/hub-site-present" yes
    else
      write_root_file "$TRANSACTION_DIR/hub-site-present" no
    fi
  fi
  write_root_file "$TRANSACTION_DIR/previous.sha" "$previous_sha"
  write_root_file "$TRANSACTION_DIR/target.sha" "$deploy_sha"
  write_root_file "$TRANSACTION_DIR/caddy-mode" "$caddy_mode"
  if [[ -f "$REPO/.dev.vars" ]]; then
    run_root cp -p -- "$REPO/.dev.vars" "$TRANSACTION_DIR/dev.vars.previous"
    write_root_file "$TRANSACTION_DIR/dev-vars-present" yes
  else
    write_root_file "$TRANSACTION_DIR/dev-vars-present" no
  fi

  on_apply_error() {
    local original_status=$?
    trap - ERR
    if ! run_root test -f "$TRANSACTION_DIR/prepared"; then
      remove_transaction_state || true
      exit "$original_status"
    fi
    printf 'ERROR: deploy failed; restoring the previous Hub2 release and protected configuration.\n' >&2
    if restore_previous_release; then
      printf 'Automatic rollback succeeded.\n' >&2
    else
      printf 'CRITICAL: automatic rollback failed; protected recovery state remains at %s.\n' \
        "$TRANSACTION_DIR" >&2
    fi
    exit "$original_status"
  }
  trap on_apply_error ERR
  write_root_file "$TRANSACTION_DIR/prepared" yes

  git -C "$REPO" checkout --detach --force "$deploy_sha"
  install_release_dependencies
  if [[ "$caddy_mode" == full ]]; then
    run_root bash "$REPO/deploy/vm/install-hub2-node.sh" --release-mode "$release_mode"
  else
    run_root bash "$REPO/deploy/vm/install-hub2-node.sh" --release-mode "$release_mode" --hub-only
  fi
  run_root systemctl is-active --quiet agent-hub-hub2.service
  wait_for_hub_health
  write_root_file "$TRANSACTION_DIR/applied" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  trap - ERR
  printf 'Hub2 candidate %s installed; rollback state retained until public smoke checks pass.\n' "$deploy_sha"
}

commit_release() {
  local target_sha current_sha
  run_root test -f "$TRANSACTION_DIR/prepared" \
    || fail "no pending deploy transaction to finalize"
  run_root test -f "$TRANSACTION_DIR/target.sha" \
    || fail "pending deploy transaction is missing its target commit"
  target_sha="$(read_root_file "$TRANSACTION_DIR/target.sha")"
  current_sha="$(git -C "$REPO" rev-parse HEAD)"
  [[ "$target_sha" == "$current_sha" ]] \
    || fail "current revision does not match the pending deploy transaction"
  remove_transaction_state
  printf 'Public smokes passed; deploy rollback state released for %s.\n' "$target_sha"
}

operation="${1:-}"
case "$operation" in
  apply)
    [[ $# == 3 || $# == 4 ]] || fail "usage: $0 apply <full-commit-sha> <bootstrap|active> [full|hub-only]"
    apply_release "$2" "$3" "${4:-full}"
    ;;
  rollback)
    [[ $# == 1 ]] || fail "usage: $0 rollback"
    restore_previous_release
    ;;
  commit)
    [[ $# == 1 ]] || fail "usage: $0 commit"
    commit_release
    ;;
  *)
    fail "operation must be apply, rollback, or commit"
    ;;
esac
