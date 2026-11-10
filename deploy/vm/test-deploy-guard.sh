#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TRANSACTION="$ROOT/deploy/vm/deploy-release-transaction.sh"
mkdir -p "$ROOT/.local/tmp"
TEST_ROOT="$(mktemp -d "$ROOT/.local/tmp/deploy-guard.XXXXXX")"
trap 'rm -rf -- "$TEST_ROOT"' EXIT

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }

WORKFLOW="$ROOT/.github/workflows/deploy.yml"
! grep -Fq 'install -m 644 "$DEPLOY_DIR/Caddyfile"' "$ROOT/deploy/vm/install-hub2-node.sh" \
  || fail 'installer must not replace the shared Caddyfile'
grep -Fq 'hub_only:' "$WORKFLOW" || fail 'missing explicit Hub-only deploy option'
grep -Fq 'https://hub.dtcdev.click/health' "$WORKFLOW" || fail 'missing Hub API read'
grep -Fq 'always() && steps.finalize_deploy.outcome' "$WORKFLOW" \
  || fail 'missing rollback step that also handles runner cancellation'
grep -Fq 'rollback_pending:' "$WORKFLOW" || fail 'missing fixed rollback recovery action'
grep -Fq 'deploy-release-transaction.sh' "$WORKFLOW" || fail 'missing fixed transaction script'
grep -Fq 'apply "$DEPLOY_SHA" "$RELEASE_MODE" "$caddy_mode"' "$WORKFLOW" \
  || fail 'workflow does not select the full or isolated Hub-only transaction path'
grep -Fq 'default: active' "$WORKFLOW" || fail 'release_mode must default to active'
python3 - "$WORKFLOW" <<'PY' || fail 'release_mode default is not active'
import re,sys
t=open(sys.argv[1]).read()
m=re.search(r'release_mode:\n(?:.*\n)*?\s+default: (\w+)',t)
sys.exit(0 if m and m.group(1)=='active' else 1)
PY
grep -Fq 'confirm_bootstrap' "$WORKFLOW" && grep -Fq 'DISABLE-HUB-DISPATCH' "$WORKFLOW" \
  || fail 'bootstrap must require a typed confirmation'
pass 'workflow covers Hub health and rollback'

mkdir -p "$TEST_ROOT/bin" "$TEST_ROOT/etc/sites"
git init --bare -q "$TEST_ROOT/remote.git"
git clone -q "$TEST_ROOT/remote.git" "$TEST_ROOT/seed" 2>/dev/null
git -C "$TEST_ROOT/seed" config user.name deploy-guard-test
git -C "$TEST_ROOT/seed" config user.email deploy-guard@example.invalid
git -C "$TEST_ROOT/seed" checkout -q -b main
mkdir -p "$TEST_ROOT/seed/deploy/vm" "$TEST_ROOT/seed/.local/tmp"

cat > "$TEST_ROOT/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
exec "$@"
SUDO
cat > "$TEST_ROOT/bin/install" <<'INSTALL'
#!/usr/bin/env bash
args=()
while (($#)); do
  case "$1" in
    -o|-g) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
exec /usr/bin/install "${args[@]}"
INSTALL
cat > "$TEST_ROOT/bin/caddy" <<'CADDY'
#!/usr/bin/env bash
[[ "$1" == validate ]] || exit 91
shift
[[ "$1" == --config ]] || exit 92
config="$2"
printf 'validate\n' >> "${CADDY_CAPTURE:?}"
if [[ "$config" == "${AGENT_HUB_TEST_CADDY_CONFIG:?}" ]]; then
  grep -Fq 'import /etc/caddy/sites/*.caddy' "$config" || exit 93
else
  grep -Fq 'reverse_proxy 127.0.0.1:8787' "$config" || exit 94
fi
CADDY
cat > "$TEST_ROOT/bin/systemctl" <<'SYSTEMCTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSTEMCTL_CAPTURE:?}"
case "$1" in
  is-active|reload|restart) exit 0 ;;
  *) exit 95 ;;
esac
SYSTEMCTL
cat > "$TEST_ROOT/bin/curl" <<'CURL'
#!/usr/bin/env bash
[[ "$*" == *'http://127.0.0.1:8787/health'* ]] || exit 96
if [[ "${TEST_CURL_FAIL_FIRST:-0}" =~ ^[0-9]+$ ]] && ((TEST_CURL_FAIL_FIRST > 0)); then
  count=0
  [[ ! -s "${CURL_COUNT_FILE:?}" ]] || read -r count < "$CURL_COUNT_FILE"
  count=$((count + 1))
  printf '%s\n' "$count" > "$CURL_COUNT_FILE"
  if ((count <= TEST_CURL_FAIL_FIRST)); then exit 7; fi
fi
printf '%s\n' '{"ok":true,"service":"hub2"}'
CURL
cat > "$TEST_ROOT/bin/sleep" <<'SLEEP'
#!/usr/bin/env bash
[[ "$#" == 1 && "$1" == 1 ]] || exit 100
printf '%s\n' "$1" >> "${SLEEP_CAPTURE:?}"
SLEEP
cat > "$TEST_ROOT/bin/npm" <<'NPM'
#!/usr/bin/env bash
[[ "$1" == --prefix && "$2" == "${AGENT_HUB_TEST_REPO:?}" ]] || exit 97
[[ "$3" == ci && "$4" == --omit=dev ]] || exit 98
printf '%s\n' "$*" >> "${NPM_CAPTURE:?}"
if [[ "${TEST_NPM_FAIL_ONCE:-0}" == 1 && ! -e "${NPM_FAIL_MARKER:?}" ]]; then
  : > "$NPM_FAIL_MARKER"
  exit 99
fi
NPM
chmod 755 "$TEST_ROOT/bin/"*

mkdir -p "$TEST_ROOT/seed/deploy" "$TEST_ROOT/seed/.local/tmp"
cat > "$TEST_ROOT/seed/deploy/hub.caddy" <<'CADDY_OLD'
:80 {
  reverse_proxy 127.0.0.1:8787
}
# previous
CADDY_OLD
cat > "$TEST_ROOT/seed/deploy/vm/install-hub2-node.sh" <<'INSTALLER'
#!/usr/bin/env bash
set -euo pipefail
[[ "$PWD" == "${AGENT_HUB_TEST_REPO:?}" ]] || exit 89
hub_only=false
release_mode=""
while (($#)); do
  case "$1" in
    --hub-only) hub_only=true; shift ;;
    --release-mode) release_mode="$2"; shift 2 ;;
    *) exit 88 ;;
  esac
done
if [[ "${TEST_INSTALL_FAIL:-0}" == 1 && -n "$release_mode" ]]; then exit 23; fi
if [[ "$hub_only" != true ]]; then
  cp "$AGENT_HUB_TEST_REPO/deploy/hub.caddy" "$AGENT_HUB_TEST_CADDY_SITE"
fi
if [[ -n "$release_mode" ]]; then printf 'HUB2_RELEASE_MODE=%s\n' "$release_mode" > "$AGENT_HUB_TEST_REPO/.dev.vars"; fi
INSTALLER
chmod 755 "$TEST_ROOT/seed/deploy/vm/install-hub2-node.sh"
cat > "$TEST_ROOT/seed/.gitignore" <<'IGNORE'
.dev.vars
.local/
IGNORE
git -C "$TEST_ROOT/seed" add .
git -C "$TEST_ROOT/seed" commit -qm 'previous release'
git -C "$TEST_ROOT/seed" push -q "$TEST_ROOT/remote.git" main
git --git-dir="$TEST_ROOT/remote.git" symbolic-ref HEAD refs/heads/main
git clone -q "$TEST_ROOT/remote.git" "$TEST_ROOT/work"
printf 'HUB2_RELEASE_MODE=bootstrap\nHUB2_CREDENTIAL_KEY=preserve-this\n' > "$TEST_ROOT/work/.dev.vars"
cp "$TEST_ROOT/work/deploy/hub.caddy" "$TEST_ROOT/etc/sites/hub.caddy"
cat > "$TEST_ROOT/etc/Caddyfile" <<'SHARED'
import /etc/caddy/sites/*.caddy
# aws-infra shared sentinel
SHARED
cp "$TEST_ROOT/etc/Caddyfile" "$TEST_ROOT/shared.original"

cat > "$TEST_ROOT/seed/deploy/hub.caddy" <<'CADDY_NEW'
:80 {
  reverse_proxy 127.0.0.1:8787
}
# candidate
CADDY_NEW
git -C "$TEST_ROOT/seed" add deploy/hub.caddy
git -C "$TEST_ROOT/seed" commit -qm 'candidate release'
git -C "$TEST_ROOT/seed" push -q origin main
CANDIDATE_SHA="$(git -C "$TEST_ROOT/seed" rev-parse HEAD)"
PREVIOUS_SHA="$(git -C "$TEST_ROOT/work" rev-parse HEAD)"

run_transaction() {
  env AGENT_HUB_DEPLOY_TEST_MODE=1 \
    AGENT_HUB_TEST_REPO="$TEST_ROOT/work" \
    AGENT_HUB_TEST_TRANSACTION_DIR="$TEST_ROOT/state" \
    AGENT_HUB_TEST_CADDY_CONFIG="$TEST_ROOT/etc/Caddyfile" \
    AGENT_HUB_TEST_CADDY_SITE="$TEST_ROOT/etc/sites/hub.caddy" \
    CADDY_CAPTURE="$TEST_ROOT/caddy-calls" \
    SYSTEMCTL_CAPTURE="$TEST_ROOT/systemctl-calls" \
    NPM_CAPTURE="$TEST_ROOT/npm-calls" \
    CURL_COUNT_FILE="$TEST_ROOT/curl-count" \
    SLEEP_CAPTURE="$TEST_ROOT/sleep-calls" \
    NPM_FAIL_MARKER="$TEST_ROOT/npm-failed-once" \
    PATH="$TEST_ROOT/bin:$PATH" "$TRANSACTION" "$@"
}

run_transaction apply "$CANDIDATE_SHA" active >/dev/null
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$CANDIDATE_SHA" ]] || fail 'apply did not select candidate'
grep -Fq '# candidate' "$TEST_ROOT/etc/sites/hub.caddy" || fail 'candidate Caddy config was not installed'
grep -Fq 'HUB2_RELEASE_MODE=active' "$TEST_ROOT/work/.dev.vars" || fail 'release mode was not set'
[[ -f "$TEST_ROOT/state/prepared" ]] || fail 'rollback state was removed before public smokes'
pass 'apply retains rollback state until public smokes finish'

: > "$TEST_ROOT/curl-count"
: > "$TEST_ROOT/sleep-calls"
TEST_CURL_FAIL_FIRST=2 run_transaction rollback >/dev/null
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'manual rollback missed old release'
cmp -s "$TEST_ROOT/etc/sites/hub.caddy" "$TEST_ROOT/work/deploy/hub.caddy" || fail 'manual rollback missed old Caddyfile'
grep -Fq 'HUB2_CREDENTIAL_KEY=preserve-this' "$TEST_ROOT/work/.dev.vars" || fail 'rollback missed private vars'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'successful rollback retained transaction state'
[[ "$(cat "$TEST_ROOT/curl-count")" == 3 ]] || fail 'rollback did not retry transient Hub startup failures'
[[ "$(wc -l < "$TEST_ROOT/sleep-calls")" == 2 ]] || fail 'rollback did not wait between Hub health retries'
grep -Fq -- "--prefix $TEST_ROOT/work ci --omit=dev" "$TEST_ROOT/npm-calls" \
  || fail 'dependency installation did not target the transactional repository'
pass 'manual rollback restores release, Caddy config, and .dev.vars'

ORIGINAL_CADDY_SHA256="$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)"
: > "$TEST_ROOT/caddy-calls"
: > "$TEST_ROOT/systemctl-calls"
run_transaction apply "$CANDIDATE_SHA" active hub-only >/dev/null
[[ "$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)" == "$ORIGINAL_CADDY_SHA256" ]] \
  || fail 'Hub-only apply changed the Caddyfile'
[[ "$(cat "$TEST_ROOT/state/caddy-mode")" == hub-only ]] || fail 'Hub-only transaction mode was not recorded'
[[ ! -s "$TEST_ROOT/caddy-calls" ]] || fail 'Hub-only apply ran Caddy validation'
run_transaction rollback >/dev/null
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'Hub-only rollback missed old release'
[[ "$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)" == "$ORIGINAL_CADDY_SHA256" ]] \
  || fail 'Hub-only rollback changed the Caddyfile'
if grep -Eq '(^| )(reload|restart) caddy($| )' "$TEST_ROOT/systemctl-calls"; then
  fail 'Hub-only apply or rollback reloaded/restarted Caddy'
fi
pass 'Hub-only apply and rollback leave the shared Caddy configuration untouched'

if TEST_NPM_FAIL_ONCE=1 run_transaction apply "$CANDIDATE_SHA" active >/dev/null 2>&1; then
  fail 'dependency installation failure unexpectedly succeeded'
fi
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] \
  || fail 'dependency installation failure did not restore old release'
cmp -s "$TEST_ROOT/etc/sites/hub.caddy" "$TEST_ROOT/work/deploy/hub.caddy" \
  || fail 'dependency installation failure did not restore old Caddyfile'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'successful dependency rollback retained transaction state'
pass 'dependency installation failure automatically restores previous release and config'

if TEST_INSTALL_FAIL=1 run_transaction apply "$CANDIDATE_SHA" active >/dev/null 2>&1; then
  fail 'installer failure unexpectedly succeeded'
fi
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'automatic rollback missed old release'
cmp -s "$TEST_ROOT/etc/sites/hub.caddy" "$TEST_ROOT/work/deploy/hub.caddy" || fail 'automatic rollback missed old Caddyfile'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'automatic rollback retained transaction state'
pass 'installer failure rolls back before returning failure'

cat > "$TEST_ROOT/seed/deploy/hub.caddy" <<'CADDY_BAD'
:80 {
  reverse_proxy 127.0.0.1:9999
}
CADDY_BAD
git -C "$TEST_ROOT/seed" add deploy/hub.caddy
git -C "$TEST_ROOT/seed" commit -qm 'invalid candidate without Hub upstream'
git -C "$TEST_ROOT/seed" push -q origin main
BAD_SHA="$(git -C "$TEST_ROOT/seed" rev-parse HEAD)"
if run_transaction apply "$BAD_SHA" active >/dev/null 2>&1; then
  fail 'candidate without Hub upstream passed pre-deploy validation'
fi
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'bad candidate changed checkout'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'bad candidate created rollback state'
pass 'bad Caddy candidate is rejected before changing the live release'

cmp -s "$TEST_ROOT/etc/Caddyfile" "$TEST_ROOT/shared.original" \
  || fail 'Hub transaction changed shared Caddyfile'
printf '# missing import\n' > "$TEST_ROOT/etc/Caddyfile"
if run_transaction apply "$CANDIDATE_SHA" active >/dev/null 2>&1; then
  fail 'shared config without sites import passed pre-deploy validation'
fi
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'missing import changed checkout'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'missing import created rollback state'
pass 'shared Caddyfile is preserved and missing import fails before changing release'

printf 'VM deploy guard tests passed.\n'
