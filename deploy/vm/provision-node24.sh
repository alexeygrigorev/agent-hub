#!/usr/bin/env bash
# Maintained Node24 runtime provisioning for the Hub2 VM.
# CI stages these exact bytes over normal SSH and the deployment transaction
# invokes them INSIDE its own single deploy lock, before any source, npm,
# configuration or systemd-unit mutation; the standalone mode keeps the same
# lock discipline for direct invocation. This is a maintained
# deployment-custody action, never a manual operational prerequisite and
# never a curl-piped upstream script: the only downloaded bytes are the
# approved NodeSource signing key, and the package artifact is fetched,
# hash-verified and installed by apt itself against the signed nodistro
# index. The script never runs a general OS upgrade, never overwrites a
# foreign apt source or keyring and never downgrades an installed package.
#
# Pure stubbed-command tests: test/test_node24_runtime.py. Production paths
# are fixed below; tests may relocate ONLY the absolute paths through the
# AGENT_HUB_NODE24_TEST_* seams, and production CI must leave those unset.
set -Eeuo pipefail

# === Reviewed Node24 runtime binding (update only through review) ==========
# Release facts verified 2026-11-18 against the signed nodistro index:
# https://deb.nodesource.com/node_24.x/dists/nodistro/{main/binary-<arch>/Packages,Release,Release.gpg}
# and the Release signature verified against the key fetched from
# $NODESOURCE_KEY_URL (primary fingerprint below). Artifact SHA-256 sums are
# enforced by apt through that signed Release/Packages chain and re-checked
# here against the downloaded archive before installation.
REQUIRED_NODE_MAJOR=24
NODE_PACKAGE=nodejs
NODE_PACKAGE_VERSION='24.22.3-1nodesource1'
NODE_PACKAGE_SHA256_AMD64='583ea87b516887e93e075b06115c5b9ad74b02e1dcb94eadc8ebff5978d82dd4'
NODE_PACKAGE_SHA256_ARM64='bdf8027e9ac30d467a7ec47e9cfa3f162c65a696a9e31a42876cd52512d61646'
NODESOURCE_KEY_URL='https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key'
NODESOURCE_KEY_FINGERPRINT='6F71F525282841EEDAF851B42F59B5F99B1BE0B4'
NODESOURCE_REPO='https://deb.nodesource.com/node_24.x'
NODESOURCE_SUITE='nodistro'
NODESOURCE_COMPONENT='main'

# === Fixed production paths (test seams must stay unset in production) ======
SERVICE_NODE="${AGENT_HUB_NODE24_TEST_SERVICE_NODE:-/usr/bin/node}"
OS_RELEASE="${AGENT_HUB_NODE24_TEST_OS_RELEASE:-/etc/os-release}"
KEYRING="${AGENT_HUB_NODE24_TEST_KEYRING:-/usr/share/keyrings/nodesource.gpg}"
SOURCES_FILE="${AGENT_HUB_NODE24_TEST_SOURCES_FILE:-/etc/apt/sources.list.d/nodesource-node24.list}"
APT_ARCHIVE_DIR="${AGENT_HUB_NODE24_TEST_APT_CACHE:-/var/cache/apt/archives}"
REPO="${AGENT_HUB_NODE24_TEST_REPO:-/opt/agent-hub}"

PROVISION_WORK=""
ARTIFACT_PATH=""
cleanup_work() {
  if [[ -n "$PROVISION_WORK" ]]; then
    rm -rf -- "$PROVISION_WORK"
  fi
}
trap cleanup_work EXIT

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run_root() {
  if [[ "$(id -u)" == 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

# Primary-key fingerprints of a (binary or armored) OpenPGP key file. The fpr
# record directly following each pub record is the primary-key fingerprint.
keyring_primary_fingerprints() {
  local file="$1"
  gpg --batch --show-keys --with-colons --fixed-list-mode "$file" 2>/dev/null \
    | awk -F: '/^pub:/{want=1; next} want && /^fpr:/{print $10; want=0}'
}

# The approved key must be the ONLY primary key in the material we install or
# accept as already installed.
key_material_matches_binding() {
  local file="$1" fingerprints
  fingerprints="$(keyring_primary_fingerprints "$file")" || return 1
  [[ -n "$fingerprints" ]] || return 1
  [[ "$(printf '%s\n' "$fingerprints" | sort -u | tr -d '\n')" == "$NODESOURCE_KEY_FINGERPRINT" ]]
}

node_version_major() {
  local version
  version="$("$1" --version)" || return 1
  [[ "$version" =~ ^v([0-9]+)\. ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

installed_package_version() {
  dpkg-query -W -f='${Version}' "$NODE_PACKAGE" 2>/dev/null || true
}

package_owns_service_node() {
  local owner
  owner="$(dpkg-query -S "$SERVICE_NODE" 2>/dev/null | cut -d: -f1 | head -n1)" || return 1
  [[ "$owner" == "$NODE_PACKAGE" ]]
}

validate_bindings() {
  # Refuse malformed reviewed bindings before any command with effects.
  [[ "$NODE_PACKAGE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+-1nodesource1$ ]] \
    || fail "malformed selected package version binding: $NODE_PACKAGE_VERSION"
  [[ "$NODESOURCE_KEY_FINGERPRINT" =~ ^[0-9A-F]{40}$ ]] \
    || fail "malformed approved key fingerprint binding"
  [[ "$NODE_PACKAGE_SHA256_AMD64" =~ ^[0-9a-f]{64}$ ]] \
    || fail "malformed amd64 artifact hash binding"
  [[ "$NODE_PACKAGE_SHA256_ARM64" =~ ^[0-9a-f]{64}$ ]] \
    || fail "malformed arm64 artifact hash binding"
  [[ "$NODESOURCE_REPO" == https://deb.nodesource.com/node_24.x ]] \
    || fail "unexpected approved package origin binding: $NODESOURCE_REPO"
}

detect_supported_platform() {
  local os_id os_codename
  [[ -r "$OS_RELEASE" ]] || fail "cannot read OS identity at $OS_RELEASE"
  os_id="$(sed -n 's/^ID=//p' "$OS_RELEASE" | head -n1 | tr -d '"')"
  os_codename="$(sed -n 's/^VERSION_CODENAME=//p' "$OS_RELEASE" | head -n1 | tr -d '"')"
  case "${os_id}:${os_codename}" in
    ubuntu:noble|debian:bookworm) ;;
    *) fail "unsupported OS for the approved Node24 package origin: ID=${os_id:-unknown} VERSION_CODENAME=${os_codename:-unknown}; supported: ubuntu noble (24.04), debian bookworm (12)" ;;
  esac
  PLATFORM_ARCH="$(dpkg --print-architecture 2>/dev/null)" \
    || fail "cannot determine the dpkg package architecture"
  case "$PLATFORM_ARCH" in
    amd64|arm64) ;;
    *) fail "unsupported package architecture for the approved Node24 binding: $PLATFORM_ARCH; supported: amd64, arm64" ;;
  esac
}

selected_artifact_sha256() {
  case "$PLATFORM_ARCH" in
    amd64) printf '%s\n' "$NODE_PACKAGE_SHA256_AMD64" ;;
    arm64) printf '%s\n' "$NODE_PACKAGE_SHA256_ARM64" ;;
    *) return 1 ;;
  esac
}

# Read-only convergence probe. Exits 0 with a no-op receipt only when the
# absolute service binary is major 24 AND its package identity matches the
# selected binding exactly.
already_provisioned() {
  local service_major installed_version
  [[ -x "$SERVICE_NODE" ]] || return 1
  service_major="$(node_version_major "$SERVICE_NODE")" || return 1
  [[ "$service_major" == "$REQUIRED_NODE_MAJOR" ]] || return 1
  installed_version="$(installed_package_version)"
  [[ "$installed_version" == "$NODE_PACKAGE_VERSION" ]] || return 1
  package_owns_service_node || return 1
  return 0
}

refuse_broken_identity() {
  local installed_version service_major
  installed_version="$(installed_package_version)"
  if [[ -n "$installed_version" && "$installed_version" == "$NODE_PACKAGE_VERSION" ]]; then
    if [[ -x "$SERVICE_NODE" ]] \
      && service_major="$(node_version_major "$SERVICE_NODE")" \
      && [[ "$service_major" == "$REQUIRED_NODE_MAJOR" ]]; then
      fail "selected package $NODE_PACKAGE=$NODE_PACKAGE_VERSION is installed but $SERVICE_NODE is not owned by it; operator must restore the package-owned runtime before provisioning"
    fi
    fail "selected package $NODE_PACKAGE=$NODE_PACKAGE_VERSION is installed but $SERVICE_NODE does not report Node24 (broken runtime); operator reconciliation required before provisioning"
  fi
  if [[ -n "$installed_version" && "$installed_version" =~ ^24\. ]]; then
    fail "installed Node24 package $NODE_PACKAGE=$installed_version differs from the selected binding $NODE_PACKAGE_VERSION; refusing an implicit package downgrade or side-grade"
  fi
}

validate_downloaded_key() {
  # Never write keyring material before this validation passes.
  [[ -s "$PROVISION_WORK/repo.key" ]] || fail "downloaded NodeSource key is empty"
  [[ "$(wc -c < "$PROVISION_WORK/repo.key")" -le 65536 ]] \
    || fail "downloaded NodeSource key exceeds the bounded size"
  gpg --batch --yes --dearmor < "$PROVISION_WORK/repo.key" > "$PROVISION_WORK/repo.gpg" \
    || fail "downloaded NodeSource key is not valid OpenPGP material"
  key_material_matches_binding "$PROVISION_WORK/repo.gpg" \
    || fail "downloaded NodeSource key does not match the approved fingerprint $NODESOURCE_KEY_FINGERPRINT; refusing to install unapproved signing material"
}

install_keyring() {
  # The keyring is dedicated, explicitly owned material. Anything already
  # present — foreign key bytes, a mutated copy, a symlink or any other
  # non-regular entry — is preserved byte for byte and refused; only a truly
  # absent path receives the validated download. -L must lead -e: a dangling
  # symlink would otherwise fall through to a write through the link.
  if [[ -L "$KEYRING" || -e "$KEYRING" ]]; then
    if [[ -f "$KEYRING" && ! -L "$KEYRING" ]] && key_material_matches_binding "$KEYRING"; then
      printf 'Node24 provision: approved keyring already installed at %s.\n' "$KEYRING"
      return 0
    fi
    fail "$KEYRING already exists with foreign, mutated or non-regular key material; refusing to overwrite an existing keyring (operator reconciliation required)"
  fi
  run_root install -m 644 "$PROVISION_WORK/repo.gpg" "$KEYRING" \
    || fail "could not install the validated NodeSource keyring at $KEYRING"
  key_material_matches_binding "$KEYRING" \
    || fail "installed NodeSource keyring at $KEYRING failed re-verification"
}

expected_sources_line() {
  printf 'deb [arch=%s signed-by=%s] %s %s %s\n' \
    "$PLATFORM_ARCH" "$KEYRING" "$NODESOURCE_REPO" "$NODESOURCE_SUITE" "$NODESOURCE_COMPONENT"
}

install_sources_entry() {
  local expected current
  expected="$(expected_sources_line)"
  # Same custody rule as the keyring: a symlink or any non-regular entry is
  # preserved and refused before reading, never dereferenced or written through.
  if [[ -L "$SOURCES_FILE" || -e "$SOURCES_FILE" ]]; then
    if [[ ! -f "$SOURCES_FILE" || -L "$SOURCES_FILE" ]]; then
      fail "$SOURCES_FILE is a symlink or non-regular entry; refusing to dereference or overwrite (operator reconciliation required)"
    fi
    current="$(cat "$SOURCES_FILE")" \
      || fail "cannot read the existing NodeSource apt source at $SOURCES_FILE"
    if [[ "$current" == "$expected" ]]; then
      printf 'Node24 provision: approved apt source already bound at %s.\n' "$SOURCES_FILE"
      return 0
    fi
    fail "$SOURCES_FILE already exists with unexpected content; refusing to overwrite a foreign or mutated apt source (operator reconciliation required)"
  fi
  [[ -d "$(dirname "$SOURCES_FILE")" ]] \
    || fail "$(dirname "$SOURCES_FILE") is missing; refusing to create an unexpected apt source layout"
  printf '%s\n' "$expected" > "$PROVISION_WORK/nodesource-node24.list"
  run_root install -m 644 "$PROVISION_WORK/nodesource-node24.list" "$SOURCES_FILE" \
    || fail "could not install the NodeSource apt source at $SOURCES_FILE"
}

refresh_package_index() {
  run_root env DEBIAN_FRONTEND=noninteractive timeout 300 apt-get update \
    || fail "apt-get update failed; refusing to install Node24 without a refreshed signed index (no package mutation was performed)"
}

verify_candidate_origin() {
  local policy pin_escaped
  pin_escaped="${NODE_PACKAGE_VERSION//./\\.}"
  policy="$(apt-cache policy "$NODE_PACKAGE")" \
    || fail "apt-cache policy failed; cannot bind the candidate $NODE_PACKAGE version to the approved origin"
  printf '%s\n' "$policy" | grep -Eq "^[[:space:]]*Candidate: ${pin_escaped}\$" \
    || fail "apt candidate for $NODE_PACKAGE is not the selected version $NODE_PACKAGE_VERSION; refusing to install an unselected version"
  local candidate_block
  candidate_block="$(printf '%s\n' "$policy" | grep -E -A3 "^[[:space:]]*(\*\*\* )?${pin_escaped}[[:space:]]+[0-9]+\$")" \
    || fail "selected version $NODE_PACKAGE_VERSION is not offered by any configured apt origin"
  printf '%s\n' "$candidate_block" | grep -Fq "$NODESOURCE_REPO" \
    || fail "selected version $NODE_PACKAGE_VERSION is not offered from the approved origin $NODESOURCE_REPO; refusing a foreign origin"
}

download_and_verify_artifact() {
  run_root env DEBIAN_FRONTEND=noninteractive timeout 300 \
    apt-get -y --no-install-recommends --download-only install "$NODE_PACKAGE=$NODE_PACKAGE_VERSION" \
    || fail "apt download of $NODE_PACKAGE=$NODE_PACKAGE_VERSION failed; no package was installed"
  ARTIFACT_PATH="$APT_ARCHIVE_DIR/${NODE_PACKAGE}_${NODE_PACKAGE_VERSION}_${PLATFORM_ARCH}.deb"
  [[ -f "$ARTIFACT_PATH" ]] \
    || fail "apt did not retain the downloaded archive at $ARTIFACT_PATH; refusing to install an unverifiable artifact"
  verify_artifact_hash "$ARTIFACT_PATH" \
    || fail "downloaded Node24 archive hash mismatch: bound $(selected_artifact_sha256), downloaded artifact at $ARTIFACT_PATH does not match; refusing to install the artifact"
}

# The bound SHA-256 of the selected artifact, checked against the file that is
# actually about to be consumed.
verify_artifact_hash() {
  local expected actual
  expected="$(selected_artifact_sha256)"
  actual="$(sha256sum "$1")" || fail "could not hash the Node24 archive at $1"
  actual="${actual%% *}"
  [[ "$actual" == "$expected" ]]
}

install_verified_artifact() {
  # Consume the exact verified local artifact: the operand handed to apt is
  # the archive file itself, so the selected package can never be re-resolved
  # or re-downloaded from a different byte source. The hash is re-checked at
  # consumption, immediately before the single bounded install; dependency
  # resolution still comes from the signed nodistro index. Failures are
  # receipts, never retries and never a blind downgrade.
  [[ -n "$ARTIFACT_PATH" && -f "$ARTIFACT_PATH" ]] \
    || fail "the verified Node24 archive is missing at $ARTIFACT_PATH; refusing to install a substitute"
  verify_artifact_hash "$ARTIFACT_PATH" \
    || fail "the Node24 archive at $ARTIFACT_PATH changed between verification and consumption; refusing to install an unverified artifact"
  run_root env DEBIAN_FRONTEND=noninteractive timeout 600 \
    apt-get -y --no-install-recommends install "$ARTIFACT_PATH" \
    || fail "apt install of the verified Node24 artifact failed; package manager owns recovery, application rollback does not restore the OS runtime"
}

verify_provisioned_runtime() {
  package_owns_service_node \
    || fail "post-install check failed: $SERVICE_NODE is not owned by the $NODE_PACKAGE package"
  local installed_version service_major path_node path_major
  installed_version="$(installed_package_version)"
  [[ "$installed_version" == "$NODE_PACKAGE_VERSION" ]] \
    || fail "post-install check failed: installed $NODE_PACKAGE version $installed_version does not match the selected binding $NODE_PACKAGE_VERSION"
  service_major="$(node_version_major "$SERVICE_NODE")" \
    || fail "post-install check failed: cannot probe $SERVICE_NODE --version"
  [[ "$service_major" == "$REQUIRED_NODE_MAJOR" ]] \
    || fail "post-install check failed: $SERVICE_NODE is Node $service_major, not Node$REQUIRED_NODE_MAJOR"
  path_node="$(command -v node)" \
    || fail "post-install check failed: no node on PATH for the npm dependency build"
  path_major="$(node_version_major "$path_node")" \
    || fail "post-install check failed: cannot probe the PATH node at $path_node"
  [[ "$path_major" == "$REQUIRED_NODE_MAJOR" ]] \
    || fail "post-install check failed: the npm dependency build would run on Node $path_major ($path_node), not Node$REQUIRED_NODE_MAJOR"
}

main() {
  validate_bindings
  detect_supported_platform

  if already_provisioned; then
    printf 'Node24 provision: no-op — %s reports Node %s, owned by %s=%s; the selected runtime binding is already satisfied and no package mutation was performed.\n' \
      "$SERVICE_NODE" "$("$SERVICE_NODE" --version)" "$NODE_PACKAGE" "$NODE_PACKAGE_VERSION"
    exit 0
  fi
  refuse_broken_identity

  # Mutation phase under the single deploy-lock owner. Standalone runs acquire
  # the same non-blocking transaction lock here. When the deploy transaction
  # invokes this script inside its already-held lock, custody is never trusted
  # from an environment string or mere contention: the child must inherit the
  # parent's actual open-file-description (AGENT_HUB_NODE24_LOCK_FD) bound to
  # the fixed DEPLOY_LOCK inode, proven by both (a) an independent OFD failing
  # with EAGAIN/EWOULDBLOCK and (b) non-blocking LOCK_EX on the inherited FD
  # succeeding (same OFD).
  DEPLOY_LOCK="$REPO/.local/tmp/.deploy-transaction.lock"
  mkdir -p "$REPO/.local/tmp"
  chmod 750 "$REPO/.local/tmp"
  if [[ -n "${AGENT_HUB_NODE24_LOCK_INHERITED:-}" || -n "${AGENT_HUB_NODE24_LOCK_FD:-}" ]]; then
    [[ "${AGENT_HUB_NODE24_LOCK_INHERITED:-}" == "$DEPLOY_LOCK" ]] \
      || fail "provisioner was invoked with a foreign lock custody path: ${AGENT_HUB_NODE24_LOCK_INHERITED:-<unset>}"
    [[ "${AGENT_HUB_NODE24_LOCK_FD:-}" =~ ^[0-9]+$ && "${AGENT_HUB_NODE24_LOCK_FD}" -ge 3 ]] \
      || fail "provisioner was invoked without a valid inherited deploy-lock descriptor"
    python3 - "$DEPLOY_LOCK" "$AGENT_HUB_NODE24_LOCK_FD" <<'PY' \
      || fail "deploy-lock custody was not inherited: $DEPLOY_LOCK descriptor ${AGENT_HUB_NODE24_LOCK_FD} is not the held transaction lock"
import errno, fcntl, os, stat, sys

lock_path = sys.argv[1]
lock_fd = int(sys.argv[2])

try:
    path_stat = os.lstat(lock_path)
    fd_stat = os.fstat(lock_fd)
except OSError:
    sys.exit(1)

if not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(fd_stat.st_mode):
    sys.exit(1)
if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
    sys.exit(1)

probe_fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
try:
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
            sys.exit(1)
    else:
        sys.exit(1)
finally:
    os.close(probe_fd)

try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
PY
  else
    [[ ! -L "$DEPLOY_LOCK" ]] || fail "deploy lock path $DEPLOY_LOCK is a symlink; refusing to lock"
    exec 9>>"$DEPLOY_LOCK"
    flock -n 9 || fail "another Hub2 deploy transaction operation is running; provisioning Node24 concurrently is not allowed"
  fi

  PROVISION_WORK="$(mktemp -d "$REPO/.local/tmp/provision-node24.XXXXXXXX")"

  timeout 120 curl -fsS --max-time 60 -o "$PROVISION_WORK/repo.key" "$NODESOURCE_KEY_URL" \
    || fail "could not download the approved NodeSource signing key from $NODESOURCE_KEY_URL"
  validate_downloaded_key
  install_keyring
  install_sources_entry
  refresh_package_index
  verify_candidate_origin
  download_and_verify_artifact
  install_verified_artifact
  verify_provisioned_runtime

  printf 'Node24 provision: %s is Node %s, owned by %s=%s from %s (%s nodistro %s); runtime ready for the deploy transaction.\n' \
    "$SERVICE_NODE" "$("$SERVICE_NODE" --version)" "$NODE_PACKAGE" "$NODE_PACKAGE_VERSION" \
    "$NODESOURCE_REPO" "$PLATFORM_ARCH" "$NODESOURCE_SUITE"
}

main "$@"
