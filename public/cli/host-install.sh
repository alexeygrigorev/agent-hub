#!/bin/sh
# curl -fsSL https://hub.dtcdev.click/cli/host-install.sh | sh
# All writes and runtimes are per-user. Never invokes sudo.
set -eu
umask 077
BASE="${HUB_BASE_URL:-https://hub.dtcdev.click}"
BASE="${BASE%/}"
ROOT="$HOME/.local/share/agent-hub"
BIN="$ROOT/bin"
mkdir -p "$BIN" "$ROOT/tmp"
WORK="$(mktemp -d "$ROOT/tmp/install.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT HUP INT TERM
fetch() {
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 "$1" -o "$2"
    else wget -q "$1" -O "$2"; fi
}
if [ ! -x "$BIN/uv" ]; then
    fetch https://astral.sh/uv/0.13.6/install.sh "$WORK/uv-install.sh"
    UV_UNMANAGED_INSTALL="$BIN" sh "$WORK/uv-install.sh"
fi
export PATH="$BIN:$PATH"
export UV_PYTHON_INSTALL_DIR="$ROOT/python"
export UV_CACHE_DIR="$ROOT/cache/uv"
export TMPDIR="$ROOT/tmp"
fetch "$BASE/cli/hub.pyz" "$WORK/hub.pyz"
fetch "$BASE/cli/hub.pyz.sha256" "$WORK/hub.pyz.sha256"
"$BIN/uv" run --no-project --python 3.12 python -c '
import hashlib,pathlib,sys
p=pathlib.Path(sys.argv[1]); expected=p.with_suffix(".pyz.sha256").read_text().split()[0]
if hashlib.sha256(p.read_bytes()).hexdigest()!=expected: raise SystemExit("Hub download checksum mismatch")
sys.path.insert(0,str(p))
from agent_hub_host.bootstrap import main
main(["--archive",str(p),"--base-url",sys.argv[2],"--uv",sys.argv[3]])
' "$WORK/hub.pyz" "$BASE" "$BIN/uv"
