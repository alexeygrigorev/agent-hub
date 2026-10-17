#!/bin/sh
# Developer Hub CLI one-line installer (user-local only, no sudo, no secrets).
#
#   curl -fsSL <base>/cli/install.sh | sh
#
# Env overrides (for tests, no Hetzner paths):
#   HUB_BASE_URL     hub base, default https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev
#   HUB_INSTALL_DIR  install dir, default $HOME/.local/bin (must stay under $HOME)
#
# Installs $HUB_INSTALL_DIR/hub (a python3 zipapp launcher). Fails atomically
# on any error; never touches global dirs or the `aplexer` binary.
set -eu

BASE_DEFAULT="https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev"
BASE="${HUB_BASE_URL:-$BASE_DEFAULT}"
# Strip trailing slash.
BASE="$(printf '%s' "$BASE" | sed 's#/*$##')"

INSTALL_DIR="${HUB_INSTALL_DIR:-$HOME/.local/bin}"

fail() { printf 'hub install failed: %s\n' "$1" >&2; exit 1; }

# User dir only: refuse system/global prefixes.
case "$INSTALL_DIR" in
  "$HOME"/*) ;;
  *) fail "HUB_INSTALL_DIR must be under HOME ($HOME), got: $INSTALL_DIR" ;;
esac
case "$INSTALL_DIR" in
  /usr/*|/bin/*|/sbin/*) fail "refusing privileged install dir: $INSTALL_DIR" ;;
esac

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

# Require Python >= 3.9 (hub supports >=3.9; vendored loop is 3.9-compatible).
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || fail "python3 >= 3.9 is required (found: $(python3 --version 2>&1))"

CACHE_BASE="${XDG_CACHE_HOME:-$HOME/.cache}"
WORK_PARENT="$CACHE_BASE"
mkdir -p "$WORK_PARENT" 2>/dev/null || WORK_PARENT="$HOME/.local/tmp"
mkdir -p "$WORK_PARENT" || fail "cannot create work dir under $WORK_PARENT"
WORK="$(mktemp -d "$WORK_PARENT/hub-install.XXXXXX")" || fail "cannot create temp dir"
trap 'rm -rf "$WORK"' EXIT INT TERM

# Download pure CLI assets only (no keys, no credentials).
for f in hub.pyz hub.pyz.sha256 manifest.json; do
  curl -fsSL "$BASE/cli/$f" -o "$WORK/$f" || fail "download failed: $BASE/cli/$f"
done
[ -s "$WORK/hub.pyz" ] || fail "downloaded hub.pyz is empty"

# Verify sha256 against the published manifest (python fallback when no sha256sum).
EXPECTED="$(awk '{print $1}' "$WORK/hub.pyz.sha256")"
[ -n "$EXPECTED" ] || fail "empty sha256 file"
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$WORK/hub.pyz" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL="$(shasum -a 256 "$WORK/hub.pyz" | awk '{print $1}')"
else
  ACTUAL="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$WORK/hub.pyz")"
fi
[ "$ACTUAL" = "$EXPECTED" ] || fail "sha256 mismatch (manifest $EXPECTED vs actual $ACTUAL)"

# Atomic user-local install: only ever writes $INSTALL_DIR/hub.
mkdir -p "$INSTALL_DIR" || fail "cannot create $INSTALL_DIR"
TMP_BIN="$INSTALL_DIR/.hub.tmp.$$"
cp "$WORK/hub.pyz" "$TMP_BIN" || fail "copy failed"
chmod 755 "$TMP_BIN" || fail "chmod failed"
mv -f "$TMP_BIN" "$INSTALL_DIR/hub" || fail "install move failed"

# Sanity: installed launcher answers --help/--version without network.
"$INSTALL_DIR/hub" --help >/dev/null 2>&1 || fail "installed hub --help failed"
"$INSTALL_DIR/hub" --version >/dev/null 2>&1 || fail "installed hub --version failed"

printf 'Installed hub to %s/hub\n' "$INSTALL_DIR"
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) printf 'Add to PATH: export PATH="%s:$PATH"\n' "$INSTALL_DIR" ;;
esac
printf 'Next: hub auth login [--url %s] [--label NAME]\n' "$BASE"
