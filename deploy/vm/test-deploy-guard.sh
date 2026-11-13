#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TRANSACTION="$ROOT/deploy/vm/deploy-release-transaction.sh"
mkdir -p "$ROOT/.local/tmp"
TEST_ROOT="$(mktemp -d "$ROOT/.local/tmp/deploy-guard.XXXXXX")"
POLICY_SERVER_PID=""
stop_policy_server() {
  if [[ -n "$POLICY_SERVER_PID" ]]; then
    kill "$POLICY_SERVER_PID" 2>/dev/null || true
    wait "$POLICY_SERVER_PID"
    POLICY_SERVER_PID=""
  fi
}
cleanup() {
  local status=$?
  stop_policy_server || true
  rm -rf -- "$TEST_ROOT"
  return "$status"
}
trap cleanup EXIT

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
python3 - "$WORKFLOW" <<'PY' || fail 'pre-commit live shell canary must run after public CLI verification and before finalize_deploy commit'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
verify_idx = text.find("- name: Verify public CLI and host release assets against CI bundle")
canary_idx = text.find("- name: Run pre-commit live shell start canary on Hetzner")
finalize_idx = text.find("- name: Finalize deploy after all public smokes pass")
rollback_idx = text.find("- name: Automatically restore the previous release and Caddy config after a failed deploy or smoke")
if min(verify_idx, canary_idx, finalize_idx, rollback_idx) < 0:
    raise SystemExit(1)
if not (verify_idx < canary_idx < finalize_idx < rollback_idx):
    raise SystemExit(1)
canary_block = text[canary_idx:finalize_idx]
for required in (
    "if: (inputs.release_mode || 'active') == 'active'",
    "--print-helper-sha256",
    "StrictHostKeyChecking=yes",
    "UserKnownHostsFile=\"$canary_known_hosts\"",
    "hub-deploy-gate --candidate-sha $DEPLOY_SHA --candidate-cli-sha256 $cli_sha256 --run-token $run_token --helper-sha256 $helper_sha256",
    "verdict.get(\"candidate_sha\") != deploy_sha or verdict.get(\"host_version\") != deploy_sha",
    "verdict.get(\"run_token\") != run_token",
    "verdict.get(\"helper_sha256\") != helper_sha256",
    "verdict.get(\"hub_pyz_sha256\") != cli_sha256",
    "verdict.get(\"intake_archived\") is not True",
):
    if required not in canary_block:
        raise SystemExit(f"missing {required!r} in precommit_shell_canary step")
PY
pass 'workflow covers Hub health, pre-commit live shell canary, and rollback'

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
printf 'healthy\n' >> "${HEALTH_CAPTURE:?}"
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
    --schema-migrations) [[ "$2" == apply || "$2" == defer ]] || exit 87; shift 2 ;;
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
mkdir -p "$TEST_ROOT/seed/scripts" "$TEST_ROOT/seed/hub2/src"
cp "$ROOT/scripts/schema-rollback-preflight.py" "$TEST_ROOT/seed/scripts/schema-rollback-preflight.py"
for schema_file in control-do machine-do project-do account-do mailbox-do; do
  printf 'initializeSchema(ctx,this.db,1,[])\n' > "$TEST_ROOT/seed/hub2/src/$schema_file.ts"
done
git -C "$TEST_ROOT/seed" add .
git -C "$TEST_ROOT/seed" commit -qm 'previous release'
git -C "$TEST_ROOT/seed" push -q "$TEST_ROOT/remote.git" main
git --git-dir="$TEST_ROOT/remote.git" symbolic-ref HEAD refs/heads/main
git clone -q "$TEST_ROOT/remote.git" "$TEST_ROOT/work"
mkdir -p "$TEST_ROOT/work/.local/hub2-db"
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
# The active candidate includes the actual reviewed canonical policy helper.
mkdir -p "$TEST_ROOT/seed/config"
cp "$ROOT/scripts/apply-model-policy.py" "$TEST_ROOT/seed/scripts/apply-model-policy.py"
cp "$ROOT/scripts/prepare-model-policy-service-credential.py" "$TEST_ROOT/seed/scripts/prepare-model-policy-service-credential.py"
cp "$ROOT/config/model-policy.json" "$TEST_ROOT/seed/config/model-policy.json"
python3 - "$ROOT/deploy/vm/install-hub2-node.sh" "$TEST_ROOT/seed/deploy/vm/install-hub2-node.sh" <<'PY'
from pathlib import Path
import sys
real = Path(sys.argv[1]).read_text(encoding="utf-8")
start = real.index("wait_for_hub2_readiness() {")
end = real.index("\necho\n", start)
with Path(sys.argv[2]).open("a", encoding="utf-8") as dst:
    dst.write("\n" + real[start:end] + "\n")
PY
git -C "$TEST_ROOT/seed" add deploy/hub.caddy deploy/vm/install-hub2-node.sh scripts/apply-model-policy.py scripts/prepare-model-policy-service-credential.py config/model-policy.json
git -C "$TEST_ROOT/seed" commit -qm 'candidate release'
git -C "$TEST_ROOT/seed" push -q origin main
CANDIDATE_SHA="$(git -C "$TEST_ROOT/seed" rev-parse HEAD)"
PREVIOUS_SHA="$(git -C "$TEST_ROOT/work" rev-parse HEAD)"


# Only a disposable loopback protocol peer is started; the transaction executes
# the real protected helper and checks its receipt before every mutation.
cat > "$TEST_ROOT/policy-server.py" <<'PY'
import copy, importlib.util, json, os, signal, subprocess, sys, threading
from pathlib import Path
from types import SimpleNamespace
root, temporary = map(Path, sys.argv[1:3])
candidate = sys.argv[3]
fixture_path = root / 'scripts/tests/test_schema_release_transaction.py'
if not fixture_path.exists():
    fixture_path = root / 'test/test_schema_release_transaction.py'
spec = importlib.util.spec_from_file_location('deploy_guard_policy_fixture', fixture_path)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
peer = fixture.PolicyAPI(SimpleNamespace(repo=temporary / 'work', state=temporary / 'state',
    candidate=candidate, credential=temporary / 'credentials/model-policy-admin.json',
    health_log=temporary / 'health-seen', git=lambda repo, *args: subprocess.check_output(
        ['git', '-C', str(repo), *args], text=True).strip()))
prior = copy.deepcopy(peer.current['candidates'])
prior[0]['weight'] = 37
peer.current = {'version': 5, 'candidates': prior}
try:
    credentials = temporary / 'credentials'
    credentials.mkdir(mode=0o700)
    credential = credentials / 'model-policy-admin.json'
    credential.write_text(json.dumps(fixture.service_document(target_sha=candidate)))
    credential.chmod(0o600)
    (temporary / 'policy-url.pending').write_text(peer.url)
    os.replace(temporary / 'policy-url.pending', temporary / 'policy-url')
    if not stop.wait(90):
        raise RuntimeError('deploy guard policy fixture timed out')
finally:
    peer.close()
    (temporary / 'policy-observations.json').write_text(json.dumps({
        'current': peer.current, 'prior': prior, 'writes': peer.writes(), 'issuances': peer.issuances}))
PY
python3 "$TEST_ROOT/policy-server.py" "$ROOT" "$TEST_ROOT" "$CANDIDATE_SHA" > "$TEST_ROOT/policy-server.log" 2>&1 &
POLICY_SERVER_PID=$!
python3 - "$TEST_ROOT/policy-url" <<'PY'
from pathlib import Path
import sys, time
path = Path(sys.argv[1])
deadline = time.monotonic() + 5
while not path.exists():
    if time.monotonic() >= deadline:
        raise SystemExit('offline policy API did not become ready')
    time.sleep(.01)
PY
POLICY_URL="$(cat "$TEST_ROOT/policy-url")"

run_transaction() {
  env AGENT_HUB_DEPLOY_TEST_MODE=1 \
    AGENT_HUB_TEST_REPO="$TEST_ROOT/work" \
    AGENT_HUB_TEST_TRANSACTION_DIR="$TEST_ROOT/state" \
    AGENT_HUB_TEST_CADDY_CONFIG="$TEST_ROOT/etc/Caddyfile" \
    AGENT_HUB_TEST_CADDY_SITE="$TEST_ROOT/etc/sites/hub.caddy" \
    AGENT_HUB_TEST_POLICY_URL="$POLICY_URL" \
    AGENT_HUB_TEST_POLICY_CREDENTIAL="$TEST_ROOT/credentials/model-policy-admin.json" \
    HUB2_TOKEN=ambient-token-must-not-be-used \
    HEALTH_CAPTURE="$TEST_ROOT/health-seen" \
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
python3 - "$TEST_ROOT/state" "$CANDIDATE_SHA" <<'PY'
import json, sys
from pathlib import Path
state = Path(sys.argv[1])
token = (state / 'transaction-token').read_text().strip()
receipt = json.loads((state / ('model-policy-' + token) / 'receipt.json').read_text())
assert receipt['phase'] == 'applied' and receipt['changed'] is True
assert receipt['transaction_token'] == token and receipt['source_sha'] == sys.argv[2]
assert receipt['before']['version'] == 5 and receipt['applied']['version'] == 6
PY
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
: > "$TEST_ROOT/curl-count"
: > "$TEST_ROOT/sleep-calls"
TEST_CURL_FAIL_FIRST=3 run_transaction apply "$CANDIDATE_SHA" active hub-only >/dev/null
[[ "$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)" == "$ORIGINAL_CADDY_SHA256" ]] \
  || fail 'Hub-only apply changed the Caddyfile'
[[ "$(cat "$TEST_ROOT/state/caddy-mode")" == hub-only ]] || fail 'Hub-only transaction mode was not recorded'
[[ ! -s "$TEST_ROOT/caddy-calls" ]] || fail 'Hub-only apply ran Caddy validation'
[[ "$(cat "$TEST_ROOT/curl-count")" == 5 ]] || fail 'installer readiness loop did not retry initial connect refusals'
[[ "$(wc -l < "$TEST_ROOT/sleep-calls")" == 3 ]] || fail 'installer readiness loop did not sleep between connect refusals'
run_transaction rollback >/dev/null
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] || fail 'Hub-only rollback missed old release'
[[ "$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)" == "$ORIGINAL_CADDY_SHA256" ]] \
  || fail 'Hub-only rollback changed the Caddyfile'
if grep -Eq '(^| )(reload|restart) caddy($| )' "$TEST_ROOT/systemctl-calls"; then
  fail 'Hub-only apply or rollback reloaded/restarted Caddy'
fi
pass 'Hub-only apply and rollback leave the shared Caddy configuration untouched'

: > "$TEST_ROOT/curl-count"
: > "$TEST_ROOT/sleep-calls"
if TEST_CURL_FAIL_FIRST=15 run_transaction apply "$CANDIDATE_SHA" active hub-only >/dev/null 2>"$TEST_ROOT/readiness-fail.err"; then
  fail 'permanent installer readiness failure unexpectedly succeeded'
fi
grep -Fq 'ERROR: Hub2 local health check http://127.0.0.1:8787/health did not become ready after 15 attempts /' "$TEST_ROOT/readiness-fail.err" \
  && grep -Fq '(last curl exit=7)' "$TEST_ROOT/readiness-fail.err" \
  || fail 'permanent installer readiness failure did not log loud diagnostic error'
[[ "$(git -C "$TEST_ROOT/work" rev-parse HEAD)" == "$PREVIOUS_SHA" ]] \
  || fail 'permanent installer readiness failure did not restore old release'
[[ "$(sha256sum "$TEST_ROOT/etc/Caddyfile" | cut -d' ' -f1)" == "$ORIGINAL_CADDY_SHA256" ]] \
  || fail 'permanent installer readiness failure changed shared Caddyfile'
[[ ! -e "$TEST_ROOT/state" ]] || fail 'successful readiness rollback retained transaction state'
[[ "$(cat "$TEST_ROOT/curl-count")" == 16 ]] || fail 'permanent installer readiness failure did not exhaust 15 attempts before rollback health check'
[[ "$(wc -l < "$TEST_ROOT/sleep-calls")" == 14 ]] || fail 'permanent installer readiness failure did not wait 14 times across 15 attempts'
pass 'permanent installer readiness failure logs diagnostic and automatically rolls back'

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

stop_policy_server
python3 - "$TEST_ROOT/policy-observations.json" "$CANDIDATE_SHA" <<'PY'
import json, sys
observed = json.load(open(sys.argv[1]))
assert observed['current'] == {'version': 9, 'candidates': observed['prior']}
assert observed['issuances'] == 0, 'existing service must never be replaced by a new issuance'
assert [row[0] for row in observed['writes']] == ['POST', 'PUT', 'POST', 'PUT']
assert all(row[2] == sys.argv[2] for row in observed['writes']), 'policy restore must precede old checkout'
PY
pass 'active applies use real canonical policy recovery and restore it before old checkout'

printf 'VM deploy guard tests passed.\n'
