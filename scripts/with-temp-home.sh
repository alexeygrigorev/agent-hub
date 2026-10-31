#!/usr/bin/env bash
# Run a command with a disposable HOME and TMPDIR, then remove ONLY that
# temporary directory. Incident 504d662b: `HOME=$(mktemp -d) cmd; rm -rf "$HOME"`
# removed the real home, because the prefix assignment never changed the shell's HOME.
#
#   scripts/with-temp-home.sh -- <command> [args...]
#   scripts/with-temp-home.sh --check-remove PATH   # guard self-test; removes nothing
set -uo pipefail

real_home() {
  local h
  h=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)
  printf '%s\n' "${h:-${HOME:-/}}"
}

# Exit 0 only for a temp root this script created; non-zero with a reason otherwise.
removable() {
  local target=$1 home resolved p
  home=$(real_home)
  [ -n "$target" ] || { echo "refuse: empty path" >&2; return 2; }
  resolved=$(realpath -m -- "$target")
  case "$resolved" in
    /|/home|/root|"$home") echo "refuse: $resolved is a home or root" >&2; return 2 ;;
  esac
  for p in "$home/.local" "$home/.local/bin" "$home/.local/lib" "$home/.local/share" \
           "$home/.local/state" "$home/.config" "$home/prod" "$home/git"; do
    case "$p/" in "$resolved"/*) echo "refuse: $resolved contains protected $p" >&2; return 2 ;; esac
  done
  case "$(basename -- "$resolved")" in
    with-temp-home.*) ;;
    *) echo "refuse: $resolved was not created by with-temp-home.sh" >&2; return 2 ;;
  esac
  [ -f "$resolved/.with-temp-home" ] || { echo "refuse: $resolved has no ownership marker" >&2; return 2; }
  return 0
}

if [ "${1:-}" = "--check-remove" ]; then
  removable "${2:-}"; exit $?
fi
[ "${1:-}" = "--" ] && shift
[ $# -gt 0 ] || { echo "usage: $0 -- <command> [args...]" >&2; exit 2; }

base=${TMPDIR:-/tmp}
[ "$(realpath -m -- "$base")" = "$(real_home)" ] && base=/tmp
root=$(mktemp -d "$base/with-temp-home.XXXXXXXX") || exit 1
: > "$root/.with-temp-home"
mkdir -p "$root/home" "$root/tmp"
cleanup() { if removable "$root"; then rm -rf -- "$root"; fi; }
trap cleanup EXIT
HOME="$root/home" TMPDIR="$root/tmp" XDG_CONFIG_HOME="$root/home/.config" \
  XDG_DATA_HOME="$root/home/.local/share" XDG_STATE_HOME="$root/home/.local/state" \
  XDG_CACHE_HOME="$root/home/.cache" "$@"
