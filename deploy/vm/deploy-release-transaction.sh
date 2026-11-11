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
  CLI_RELEASE_ROOT="$REPO/.local/cli-releases"
  DB_DIR="${AGENT_HUB_TEST_DB_DIR:-$REPO/.local/hub2-db}"
  POLICY_URL="${AGENT_HUB_TEST_POLICY_URL:-http://127.0.0.1:8787}"
  POLICY_CREDENTIAL="${AGENT_HUB_TEST_POLICY_CREDENTIAL:-$REPO/.local/deploy-credentials/model-policy-admin.json}"
else
  REPO=/opt/agent-hub
  TRANSACTION_DIR=/var/lib/agent-hub/deploy-rollback
  CADDY_CONFIG=/etc/caddy/Caddyfile
  CADDY_SITE=/etc/caddy/sites/hub.caddy
  CLI_RELEASE_ROOT=/var/lib/agent-hub/cli-releases
  DB_DIR=/var/lib/agent-hub/hub2-db
  POLICY_URL=http://127.0.0.1:8787
  POLICY_CREDENTIAL=/etc/agent-hub/deploy-credentials/model-policy-admin.json
fi
CADDY_BIN=caddy
TMPDIR="$REPO/.local/tmp"
LEGACY_CLI_DIR="$REPO/hub2/public/cli"
CANDIDATE_CADDY=""
SCHEMA_PREFLIGHT=""
POLICY_TRANSACTION_TOKEN=""
POLICY_SOURCE_SHA=""

cleanup_candidate() {
  if [[ -n "$SCHEMA_PREFLIGHT" ]]; then
    rm -f -- "$SCHEMA_PREFLIGHT"
  fi
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

validate_policy_credential() {
  # The helper supports --token-file, but intentionally also supports ambient
  # HUB2_TOKEN. Deploys require this explicit private credential instead.
  run_root python3 - "$POLICY_CREDENTIAL" <<'PY'
import json, os, pathlib, re, stat, sys
try:
    path = pathlib.Path(sys.argv[1])
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError()
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise ValueError()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 16384:
            raise ValueError()
        value = json.load(stream)
    if (not isinstance(value, dict) or value.get('schema') != 'hub2-model-policy-service-credential-v1'
            or not isinstance(value.get('access_token'), str) or not re.fullmatch(r'[0-9a-f]{64}', value['access_token'])):
        raise ValueError()
    credential = value.get('credential')
    if (not isinstance(credential, dict) or credential.get('kind') != 'service' or credential.get('purpose') != 'model-policy'
            or credential.get('scopes') != ['model-policy:read', 'model-policy:write']):
        raise ValueError()
except (OSError, ValueError, TypeError):
    raise SystemExit('policy_admin_credential_unavailable_or_untrusted') from None
PY
}



tracked_service_exchange() {
  local target_sha="$1" script="$2" directory
  shift 2
  directory="$(dirname "$script")"
  # This privileged launcher, rather than its shell/sudo parent, records child
  # completion. If it dies, rollback retains the candidate even before intent.
  run_root env -u HUB2_TOKEN -u HUB_TOKEN -u HUB_CONFIG_FILE -u HUB2_CONFIG -u HUB2_SERVER \
    python3 -I -S -c '
import json, os, pathlib, subprocess, sys
folder, target, token = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
status, output = 1, {"error":"credential_preparation_failed"}
try:
    result = subprocess.run([sys.executable,"-I","-S",*sys.argv[4:]], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=60)
    status = result.returncode
    if len(result.stdout) <= 65536:
        value = json.loads(result.stdout)
        if isinstance(value,dict) and set(value) <= {"status","id","error","remaining_ms"}:
            output = value
            if status == 0 and value.get("status") not in ("service_ready","service_installed"):
                status = 1
        else:
            status = 1
    else:
        status = 1
except Exception:
    status = 1
metadata = json.dumps({"target_sha":target,"transaction_token":token,"exit_status":status}).encode()
temporary = folder / (".service-exchange-finished-" + str(os.getpid()))
fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
try:
    with os.fdopen(fd,"wb") as stream:
        stream.write(metadata); stream.flush(); os.fsync(stream.fileno())
    os.link(temporary, folder / "service-exchange-finished.json", follow_symlinks=False)
    os.unlink(temporary)
    parent = os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)
finally:
    temporary.unlink(missing_ok=True)
print(json.dumps(output,sort_keys=True))
raise SystemExit(0 if status == 0 else 1)
' "$directory" "$target_sha" "$POLICY_TRANSACTION_TOKEN" "$@"
}

# Production CLI has fixed paths/URL. Only the existing disposable test mode
# injects the exported core's owned directory, loopback transport and UID.
service_credential_helper() {
  local action="$1" target_sha="$2" script="${3:--}" directory
  if [[ "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" == 1 ]]; then
    directory="$(dirname "$POLICY_CREDENTIAL")"
    local adapter
    adapter="$(cat <<'PY'
import json, os, pathlib, sys
try:
    action, target, directory, base, script = sys.argv[1:]
    source = sys.stdin.read() if script == '-' else pathlib.Path(script).read_text()
    module = {'__name__': 'protected_service_credential_helper'}
    exec(compile(source, '<verified-service-credential-helper>', 'exec'), module)
    result = module['run'](target, directory=directory, base_url=base, owner_uid=os.getuid(),
                           action=action, post_health=action == 'exchange')
    print(json.dumps(result, sort_keys=True))
except Exception as error:
    result = {'error': getattr(error, 'code', 'credential_preparation_failed')}
    if getattr(error, 'request_id', None):
        result['id'] = 'deployment:' + error.request_id
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(1) from None
PY
)"
    if [[ "$action" == exchange ]]; then
      tracked_service_exchange "$target_sha" "$script" -c "$adapter" "$action" "$target_sha" "$directory" "$POLICY_URL" "$script"
    else
      run_root env -u HUB2_TOKEN -u HUB_TOKEN -u HUB_CONFIG_FILE -u HUB2_CONFIG -u HUB2_SERVER \
        python3 -I -S -c "$adapter" "$action" "$target_sha" "$directory" "$POLICY_URL" "$script"
    fi
  elif [[ "$action" == preflight ]]; then
    run_root env -u HUB2_TOKEN -u HUB_TOKEN -u HUB_CONFIG_FILE -u HUB2_CONFIG -u HUB2_SERVER \
      python3 -I -S - preflight --target-sha "$target_sha"
  else
    tracked_service_exchange "$target_sha" "$script" "$script" exchange --target-sha "$target_sha" --post-health
  fi
}

preflight_service_credential() {
  # This source is pinned to the verified candidate object, not the old checkout.
  # Preflight is structural and must not authenticate against an older server
  # that cannot yet recognize service credentials.
  git -C "$REPO" show "$1:scripts/prepare-model-policy-service-credential.py" | service_credential_helper preflight "$1"
}

exchange_service_credential() {
  local target_sha="$1" directory
  [[ "$(git -C "$REPO" rev-parse HEAD)" == "$target_sha" ]] || return 1
  directory="$(policy_directory)"
  run_root install -d -m 700 "$directory"
  run_root install -m 600 "$REPO/scripts/prepare-model-policy-service-credential.py" "$directory/prepare-model-policy-service-credential.py"
  write_root_file "$directory/service-exchange-started" yes
  run_root sync -f "$directory"
  service_credential_helper exchange "$target_sha" "$directory/prepare-model-policy-service-credential.py"
}


service_exchange_rollback_safe() {
  local directory inspect
  # Older prepared transactions predate both tokens and credential exchange.
  if [[ -z "$POLICY_TRANSACTION_TOKEN" ]]; then return 0; fi
  directory="$(policy_directory)" || return 1
  if ! run_root test -f "$directory/service-exchange-started"; then return 0; fi
  if ! run_root test -f "$directory/service-exchange-finished.json"; then
    printf 'ERROR: service exchange process outcome unconfirmed; retaining candidate and recovery state.\n' >&2
    return 1
  fi
  inspect="$(cat <<'PY'
import json, os, pathlib, re, sys
identity = None
try:
    folder, target, token, test_mode, credential_dir, base = sys.argv[1:]
    module = {'__name__':'protected_service_credential_helper'}
    exec(compile(sys.stdin.read(),'<verified-service-credential-helper>','exec'),module)
    context = module['_TEST_OWNER'].set(os.getuid() if test_mode == '1' else 0)
    exchange = os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        module['directory_check'](exchange,private=True)
        finished = module['parse'](module['read_private'](exchange,'service-exchange-finished.json'))
        module['require'](isinstance(finished,dict) and set(finished)=={'target_sha','transaction_token','exit_status'}
            and finished['target_sha']==target and finished['transaction_token']==token
            and type(finished['exit_status']) is int)
    finally:
        os.close(exchange)
    if test_mode == '1':
        location = pathlib.Path(credential_dir)
        module['require'](location.resolve()==location,'directory_untrusted')
        fd = os.open(location,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        module['directory_check'](fd,private=True)
    else:
        fd = module['open_directory']()
    try:
        with module['locked'](fd):
            raw = module['read_private'](fd,module['INTENT'],optional=True)
            if raw is not None:
                intent = module['parse'](raw)
                module['require'](isinstance(intent,dict) and set(intent)=={
                    'schema','request_id','target_sha','base_url','label','purpose','prior_output_sha256','phase'})
                identity = module['request_id'](intent['request_id'])
                module['source_sha'](intent['target_sha'])
                module['require'](intent['schema']=='hub2-model-policy-service-intent-v1'
                    and intent['base_url']==base and intent['label']==module['LABEL']
                    and intent['purpose']=='model-policy' and intent['phase']=='issuance_started'
                    and (intent['prior_output_sha256'] is None or isinstance(intent['prior_output_sha256'],str)
                         and re.fullmatch('[0-9a-f]{64}',intent['prior_output_sha256'])))
                output = module['service'](module['read_private'](fd,module['OUTPUT']))
                received = module['service'](module['read_private'](fd,'model-policy-service-'+identity+'.received.json'))
                module['require'](output==received and output['target_sha']==intent['target_sha']
                    and output['credential']['id']=='deployment:'+identity)
    finally:
        os.close(fd)
    module['_TEST_OWNER'].reset(context)
except Exception:
    result = {'error':'service_issuance_unconfirmed'}
    if identity: result['id']='deployment:'+identity
    print(json.dumps(result,sort_keys=True))
    raise SystemExit(1) from None
PY
)"
  git -C "$REPO" show "$POLICY_SOURCE_SHA:scripts/prepare-model-policy-service-credential.py" | \
    run_root env -u HUB2_TOKEN -u HUB_TOKEN -u HUB_CONFIG_FILE -u HUB2_CONFIG -u HUB2_SERVER \
    python3 -I -S -c "$inspect" "$directory" "$POLICY_SOURCE_SHA" "$POLICY_TRANSACTION_TOKEN" \
      "${AGENT_HUB_DEPLOY_TEST_MODE:-0}" "$(dirname "$POLICY_CREDENTIAL")" "$POLICY_URL"
}

policy_directory() {
  [[ "$POLICY_TRANSACTION_TOKEN" =~ ^[0-9a-f]{32}$ ]] || return 1
  printf '%s/model-policy-%s\n' "$TRANSACTION_DIR" "$POLICY_TRANSACTION_TOKEN"
}

policy_recovery() {
  local action="$1" directory
  directory="$(policy_directory)" || return 1
  [[ "$POLICY_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || return 1
  validate_policy_credential || return 1
  run_root env -u HUB2_TOKEN -u HUB_TOKEN -u HUB_CONFIG_FILE -u HUB2_CONFIG -u HUB2_SERVER python3 "$directory/apply-model-policy.py" "$action" \
    --url "$POLICY_URL" --token-file "$POLICY_CREDENTIAL" \
    --file "$directory/candidates.json" --source-sha "$POLICY_SOURCE_SHA" \
    --receipt "$directory/receipt.json" --transaction-token "$POLICY_TRANSACTION_TOKEN"
}

apply_canonical_policy() {
  local directory
  directory="$(policy_directory)"
  run_root install -d -m 700 "$directory"
  run_root install -m 600 "$REPO/scripts/apply-model-policy.py" "$directory/apply-model-policy.py"
  run_root install -m 600 "$REPO/config/model-policy.json" "$directory/candidates.json"
  policy_recovery prepare
  # If the process/transport dies after this durable intent, only a validated
  # receipt can authorize recovery. Never infer ownership from policy equality.
  write_root_file "$TRANSACTION_DIR/policy-apply-started" yes
  run_root sync -f "$TRANSACTION_DIR"
  policy_recovery apply
}

publisher_directory() {
  [[ "$1" =~ ^[0-9a-f]{32}$ ]] || return 1
  printf '%s/publisher-%s\n' "$TRANSACTION_DIR" "$1"
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
  local token="${1:-}"
  if [[ -z "$token" ]]; then
    # Pending deploys created before transaction tokens still need recovery.
    run_root rm -rf -- "$TRANSACTION_DIR"
    return
  fi
  run_root python3 - "$TRANSACTION_DIR" "$token" <<'PY'
import fcntl
import os
from pathlib import Path
import re
import shutil
import sys

state = Path(sys.argv[1])
token = sys.argv[2]
if not re.fullmatch(r'[0-9a-f]{32}', token):
    raise SystemExit('invalid deploy transaction token')
retired = state.with_name(state.name + '.retired-' + token)
fd = os.open(state.parent / '.deploy-retire.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'rb') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if state.exists():
        if state.is_symlink() or (state / 'transaction-token').read_bytes() != (token + '\n').encode():
            raise SystemExit('refusing to retire a different deploy transaction')
        state.rename(retired)
        parent = os.open(state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    if retired.exists():
        shutil.rmtree(retired)
PY
}

restore_previous_release() {
  local previous_sha vars_present caddy_mode site_present legacy_snapshot restore_dir publication_token publisher_dir=''
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

  service_exchange_rollback_safe || {
    printf 'ERROR: service issuance unconfirmed; candidate API and protected evidence retained for operator recovery.\n' >&2
    return 1
  }

  if ! run_root test -f "$TRANSACTION_DIR/schema-preflight.py"; then
    printf 'ERROR: rollback_schema_proof_missing; keep live databases unchanged and prepare a compatible rollback build.\n' >&2
    return 1
  fi
  run_root python3 "$TRANSACTION_DIR/schema-preflight.py" --repo "$REPO" \
    --previous "$previous_sha" --candidate "$previous_sha" --data-dir "$DB_DIR" --print-mode >/dev/null || return 1

  if run_root test -f "$TRANSACTION_DIR/policy-apply-started"; then
    policy_recovery rollback || {
      printf 'ERROR: policy rollback unconfirmed; retaining candidate code and protected recovery state.\n' >&2
      return 1
    }
  fi

  if run_root test -f "$TRANSACTION_DIR/transaction-token"; then
    publication_token="$(read_root_file "$TRANSACTION_DIR/transaction-token")" || return 1
    publisher_dir="$(publisher_directory "$publication_token")" || return 1
  fi
  if [[ -n "$publisher_dir" ]] && run_root test -f "$publisher_dir/ready"; then
    # The prior revision may lack this helper. Keep the candidate copy with the
    # rollback receipt so retry also works after restoring the prior checkout.
    run_root python3 "$publisher_dir/scripts/publish-hub2-release.py" --root "$CLI_RELEASE_ROOT" \
      rollback --receipt "$TRANSACTION_DIR/cli-assets.json" --transaction-token "$publication_token" || return 1
  fi

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
  if run_root test -f "$TRANSACTION_DIR/cli-legacy-present"; then
    legacy_snapshot="$(read_root_file "$TRANSACTION_DIR/cli-legacy-present")" || return 1
    case "$legacy_snapshot" in
      yes)
        restore_dir="$(run_root mktemp -d "$TMPDIR/cli-rollback.XXXXXXXX")" || return 1
        run_root cp -a -- "$TRANSACTION_DIR/cli-legacy.previous" "$restore_dir/cli" || return 1
        run_root rm -rf -- "$LEGACY_CLI_DIR" || return 1
        run_root mv -T -- "$restore_dir/cli" "$LEGACY_CLI_DIR" || return 1
        run_root rmdir -- "$restore_dir" || return 1
        ;;
      no) run_root rm -rf -- "$LEGACY_CLI_DIR" || return 1 ;;
      *) printf 'ERROR: invalid legacy CLI rollback metadata.\n' >&2; return 1 ;;
    esac
  fi

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

  remove_transaction_state "${publication_token:-}" || return 1
  if [[ "$caddy_mode" == full ]]; then
    printf 'Rollback completed: previous Hub2 release, .dev.vars, and Hub site restored; shared Caddyfile untouched.\n'
  else
    printf 'Rollback completed: previous Hub2 release and .dev.vars restored; Caddy was left untouched.\n'
  fi
}

apply_release() {
  local deploy_sha="$1" release_mode="$2" caddy_mode="$3" cli_mode="$4" previous_sha local_status publication_token schema_mode
  local -a asset_args=()
  [[ "$deploy_sha" =~ ^[0-9a-f]{40}$ ]] || fail "deploy SHA must be a full lowercase Git commit SHA"
  [[ "$release_mode" == bootstrap || "$release_mode" == active ]] \
    || fail "release mode must be bootstrap or active"
  [[ "$caddy_mode" == full || "$caddy_mode" == hub-only ]] \
    || fail "caddy mode must be full or hub-only"
  [[ "$cli_mode" == legacy || ( "$cli_mode" == matched-cli && "$release_mode" == active ) ]] \
    || fail "matched CLI publication requires an active deploy"
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
  if [[ "$release_mode" == active ]]; then preflight_service_credential "$deploy_sha"; fi
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

  SCHEMA_PREFLIGHT="$(mktemp "$TMPDIR/hub-schema-preflight.XXXXXX")"
  git -C "$REPO" show "${deploy_sha}:scripts/schema-rollback-preflight.py" > "$SCHEMA_PREFLIGHT" \
    || fail "candidate has no reviewed schema rollback preflight"
  chmod 600 "$SCHEMA_PREFLIGHT"
  schema_mode="$(run_root python3 "$SCHEMA_PREFLIGHT" --repo "$REPO" \
    --previous "$previous_sha" --candidate "$deploy_sha" --data-dir "$DB_DIR" --print-mode)" \
    || fail "candidate cannot be safely rolled back against the current databases"
  [[ "$schema_mode" == defer || "$schema_mode" == apply ]] || fail "invalid schema preflight result"
  printf 'Schema rollback preflight passed; migration mode=%s.\n' "$schema_mode"

  run_root install -d -o root -g root -m 700 "$TRANSACTION_DIR"
  publication_token="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  POLICY_TRANSACTION_TOKEN="$publication_token"
  POLICY_SOURCE_SHA="$deploy_sha"
  write_root_file "$TRANSACTION_DIR/transaction-token" "$publication_token"
  run_root install -o root -g root -m 600 "$SCHEMA_PREFLIGHT" "$TRANSACTION_DIR/schema-preflight.py"
  write_root_file "$TRANSACTION_DIR/schema-mode" "$schema_mode"
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
  if [[ "$release_mode" == active ]]; then write_root_file "$TRANSACTION_DIR/policy-required" yes; fi
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
      remove_transaction_state "$publication_token" || true
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
  if [[ "$cli_mode" == matched-cli ]]; then
    write_root_file "$TRANSACTION_DIR/cli-assets-required" yes
    if ! run_root test -L "$CLI_RELEASE_ROOT/current"; then
      if run_root test -L "$LEGACY_CLI_DIR"; then
        printf 'ERROR: legacy CLI directory is a symlink.\n' >&2
        false
      elif run_root test -d "$LEGACY_CLI_DIR"; then
        run_root cp -a -- "$LEGACY_CLI_DIR" "$TRANSACTION_DIR/cli-legacy.previous"
        write_root_file "$TRANSACTION_DIR/cli-legacy-present" yes
      elif run_root test -e "$LEGACY_CLI_DIR"; then
        printf 'ERROR: legacy CLI path is not a directory.\n' >&2
        false
      else
        write_root_file "$TRANSACTION_DIR/cli-legacy-present" no
      fi
    fi
  fi
  write_root_file "$TRANSACTION_DIR/prepared" yes
  if [[ "$cli_mode" == matched-cli ]]; then run_root sync -f "$TRANSACTION_DIR"; fi

  git -C "$REPO" checkout --detach --force "$deploy_sha"
  install_release_dependencies
  if [[ "$cli_mode" == matched-cli ]]; then asset_args+=(--keep-cli-assets); fi
  if [[ "$caddy_mode" == full ]]; then
    run_root bash "$REPO/deploy/vm/install-hub2-node.sh" --release-mode "$release_mode" --schema-migrations "$schema_mode" "${asset_args[@]}"
  else
    run_root bash "$REPO/deploy/vm/install-hub2-node.sh" --release-mode "$release_mode" --hub-only --schema-migrations "$schema_mode" "${asset_args[@]}"
  fi
  run_root systemctl is-active --quiet agent-hub-hub2.service
  wait_for_hub_health
  run_root python3 "$TRANSACTION_DIR/schema-preflight.py" --repo "$REPO" \
    --previous "$previous_sha" --candidate "$deploy_sha" --data-dir "$DB_DIR" --print-mode >/dev/null
  if [[ "$release_mode" == active ]]; then
    exchange_service_credential "$deploy_sha"
    apply_canonical_policy
  fi
  write_root_file "$TRANSACTION_DIR/applied" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  trap - ERR
  if [[ "$schema_mode" == defer ]]; then
    printf 'Compatible bridge installed without the new schema migrations; a subsequent verified deploy can enable them.\n'
  fi
  printf 'Hub2 candidate %s installed; rollback state retained until public smoke checks pass.\n' "$deploy_sha"
}

publish_cli_release() {
  local deploy_sha="$1" digest="$2" publication_token publisher_dir
  [[ "$deploy_sha" =~ ^[0-9a-f]{40}$ && "$digest" =~ ^[0-9a-f]{64}$ ]] \
    || fail "publication requires a full commit and CI manifest SHA-256"
  run_root test -f "$TRANSACTION_DIR/applied" || fail "no applied Hub2 deploy to publish"
  run_root test -f "$TRANSACTION_DIR/cli-assets-required" || fail "the applied deploy did not request matched CLI assets"
  [[ "$(read_root_file "$TRANSACTION_DIR/target.sha")" == "$deploy_sha" && "$(git -C "$REPO" rev-parse HEAD)" == "$deploy_sha" ]] \
    || fail "publication commit must match the applied Hub2 deploy"
  run_root systemctl is-active --quiet agent-hub-hub2.service
  curl -fsS --max-time 3 http://127.0.0.1:8787/health | python3 -c '
import json, sys
health = json.load(sys.stdin)
sys.exit(0 if health.get("ok") is True and health.get("service") == "hub2" else 1)
' || fail "the active Hub2 API must be healthy before release publication"

  publication_token="$(read_root_file "$TRANSACTION_DIR/transaction-token")"
  publisher_dir="$(publisher_directory "$publication_token")"
  if ! run_root test -f "$publisher_dir/ready"; then
    run_root install -d -m 700 "$publisher_dir/scripts"
    run_root install -m 600 -t "$publisher_dir/scripts" \
      "$REPO/scripts/"{publish-hub2-release.py,build-hub2-release.py,hub2-self-update.py}
    # Rollback can fence a delayed publisher even before it writes a receipt.
    write_root_file "$publisher_dir/ready" yes
    run_root sync -f "$TRANSACTION_DIR"
  fi
  run_root python3 "$publisher_dir/scripts/publish-hub2-release.py" --root "$CLI_RELEASE_ROOT" \
    stage --bundle "$TMPDIR/ci-cli-release-$deploy_sha" --commit "$deploy_sha" --manifest-sha256 "$digest"
  run_root python3 "$publisher_dir/scripts/publish-hub2-release.py" --root "$CLI_RELEASE_ROOT" \
    activate --commit "$deploy_sha" --manifest-sha256 "$digest" --receipt "$TRANSACTION_DIR/cli-assets.json" \
    --transaction-token "$publication_token"
}

commit_release() {
  local target_sha current_sha publication_token='' publisher_dir
  run_root test -f "$TRANSACTION_DIR/prepared" \
    || fail "no pending deploy transaction to finalize"
  run_root test -f "$TRANSACTION_DIR/target.sha" \
    || fail "pending deploy transaction is missing its target commit"
  target_sha="$(read_root_file "$TRANSACTION_DIR/target.sha")"
  current_sha="$(git -C "$REPO" rev-parse HEAD)"
  [[ "$target_sha" == "$current_sha" ]] \
    || fail "current revision does not match the pending deploy transaction"
  if run_root test -f "$TRANSACTION_DIR/transaction-token"; then
    publication_token="$(read_root_file "$TRANSACTION_DIR/transaction-token")"
  fi
  if run_root test -f "$TRANSACTION_DIR/cli-assets-required"; then
    publisher_dir="$(publisher_directory "$publication_token")"
    run_root test -f "$publisher_dir/ready" || fail "the matched CLI assets were not published"
    run_root test -f "$TRANSACTION_DIR/cli-assets.json" || fail "the matched CLI assets were not published"
  fi
  if run_root test -f "$TRANSACTION_DIR/policy-required"; then
    run_root test -f "$TRANSACTION_DIR/policy-apply-started" || fail "canonical policy application was not verified"
    # The helper seals a durable committed fence under its receipt lock. Any
    # later finalization failure is forward-only: retain candidate and retry
    # commit, rather than permit a delayed privileged rollback to undo policy.
    policy_recovery commit || fail "canonical policy could not be committed; recovery state retained"
  fi
  if run_root test -f "$TRANSACTION_DIR/cli-assets-required"; then
    run_root python3 "$publisher_dir/scripts/publish-hub2-release.py" --root "$CLI_RELEASE_ROOT" \
      commit --receipt "$TRANSACTION_DIR/cli-assets.json" --transaction-token "$publication_token" \
      || fail "the matched CLI assets were not published or are no longer active"
  fi
  remove_transaction_state "$publication_token"
  printf 'Public smokes passed; deploy rollback state released for %s.\n' "$target_sha"
}

operation="${1:-}"
if [[ "$operation" =~ ^(apply|publish|rollback|commit)$ ]]; then
  [[ -d "$REPO/.git" ]] || fail "Hub repository checkout is missing: $REPO"
  mkdir -p "$TMPDIR"
  exec 9>>"$TMPDIR/.deploy-transaction.lock"
  flock -n 9 || fail "another Hub2 deploy transaction operation is running"
  # Capture identity once. A privileged child surviving this shell must never
  # read a later same-SHA transaction's token/receipt from the reused directory.
  if run_root test -f "$TRANSACTION_DIR/transaction-token"; then
    POLICY_TRANSACTION_TOKEN="$(read_root_file "$TRANSACTION_DIR/transaction-token")"
    POLICY_SOURCE_SHA="$(read_root_file "$TRANSACTION_DIR/target.sha")"
  fi
  # Bash defers these traps while waiting for a synchronous child. In
  # particular, an SSH disconnect must not release the lock while sudo's
  # installer or publisher is still writing.
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
fi
case "$operation" in
  apply)
    [[ $# -ge 3 && $# -le 5 ]] || fail "usage: $0 apply <full-commit-sha> <bootstrap|active> [full|hub-only] [matched-cli]"
    apply_release "$2" "$3" "${4:-full}" "${5:-legacy}"
    ;;
  rollback)
    [[ $# == 1 ]] || fail "usage: $0 rollback"
    restore_previous_release
    ;;
  publish)
    [[ $# == 3 ]] || fail "usage: $0 publish <full-commit-sha> <CI-manifest-sha256>"
    publish_cli_release "$2" "$3"
    ;;
  commit)
    [[ $# == 1 ]] || fail "usage: $0 commit"
    commit_release
    ;;
  *)
    fail "operation must be apply, publish, rollback, or commit"
    ;;
esac
