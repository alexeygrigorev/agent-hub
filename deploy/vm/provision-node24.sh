#!/usr/bin/env bash
# Maintained Node24 runtime provisioning for the Hub2 VM.
# Invoked through the normal production CI SSH path (deploy.yml) BEFORE the
# deployment transaction mutates source, npm dependencies, configuration or
# systemd units. This is a maintained deployment-custody action, never a
# manual operational prerequisite and never a curl-piped upstream script:
# the only downloaded bytes are the approved NodeSource signing key, and the
# package artifact is fetched, hash-verified and installed by apt itself
# against the signed nodistro index. The script never runs a general OS
# upgrade, never overwrites a foreign apt source or keyring and never
# downgrades an installed package.
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
  if [[ -f "$KEYRING" ]]; then
    if key_material_matches_binding "$KEYRING"; then
      printf 'Node24 provision: approved keyring already installed at %s.\n' "$KEYRING"
      return 0
    fi
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
  if [[ -e "$SOURCES_FILE" ]]; then
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
  local archive expected actual
  archive="$APT_ARCHIVE_DIR/${NODE_PACKAGE}_${NODE_PACKAGE_VERSION}_${PLATFORM_ARCH}.deb"
  [[ -f "$archive" ]] || fail "apt did not retain the downloaded archive at $archive; refusing to install an unverifiable artifact"
  expected="$(selected_artifact_sha256)"
  actual="$(sha256sum "$archive")" || fail "could not hash the downloaded Node24 archive"
  actual="${actual%% *}"
  [[ "$actual" == "$expected" ]] \
    || fail "downloaded Node24 archive hash mismatch: bound $expected, downloaded $actual; refusing to install the artifact"
}

install_pinned_package() {
  # Exactly one bounded install attempt; failures are receipts, never retries
  # and never a blind downgrade.
  run_root env DEBIAN_FRONTEND=noninteractive timeout 600 \
    apt-get -y --no-install-recommends install "$NODE_PACKAGE=$NODE_PACKAGE_VERSION" \
    || fail "apt install of $NODE_PACKAGE=$NODE_PACKAGE_VERSION failed; package manager owns recovery, application rollback does not restore the OS runtime"
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

  # Mutation phase. Single-owner serialization with the deploy transaction:
  # the same non-blocking lock the transaction scripts hold, acquired only
  # here (never nested) and released when this step exits.
  mkdir -p "$REPO/.local/tmp"
  chmod 750 "$REPO/.local/tmp"
  exec 9>>"$REPO/.local/tmp/.deploy-transaction.lock"
  flock -n 9 || fail "another Hub2 deploy transaction operation is running; provisioning Node24 concurrently is not allowed"

  PROVISION_WORK="$(mktemp -d "$REPO/.local/tmp/provision-node24.XXXXXXXX")"

  timeout 120 curl -fsS --max-time 60 -o "$PROVISION_WORK/repo.key" "$NODESOURCE_KEY_URL" \
    || fail "could not download the approved NodeSource signing key from $NODESOURCE_KEY_URL"
  validate_downloaded_key
  install_keyring
  install_sources_entry
  refresh_package_index
  verify_candidate_origin
  download_and_verify_artifact
  install_pinned_package
  verify_provisioned_runtime

  printf 'Node24 provision: %s is Node %s, owned by %s=%s from %s (%s nodistro %s); runtime ready for the deploy transaction.\n' \
    "$SERVICE_NODE" "$("$SERVICE_NODE" --version)" "$NODE_PACKAGE" "$NODE_PACKAGE_VERSION" \
    "$NODESOURCE_REPO" "$PLATFORM_ARCH" "$NODESOURCE_SUITE"
}

main "$@"
