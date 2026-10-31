#!/bin/sh
# curl -fsSL https://hub.dtcdev.click/host/install.sh | sh
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
    if command -v sha256sum >/dev/null 2>&1; then
        UV_SUM="$(sha256sum "$WORK/uv-install.sh")"
    elif command -v shasum >/dev/null 2>&1; then
        UV_SUM="$(shasum -a 256 "$WORK/uv-install.sh")"
    else
        UV_SUM="$(openssl dgst -sha256 "$WORK/uv-install.sh")"
    fi
    case "$UV_SUM" in
        *f7a56e7afaec49a3bcbc3ac3cdbe917e7c6ce30e0d815308a12e531aa5ef23b7*) ;;
        *) printf '%s\n' 'uv installer checksum mismatch' >&2; exit 1 ;;
    esac
    UV_UNMANAGED_INSTALL="$BIN" sh "$WORK/uv-install.sh"
fi
export PATH="$BIN:$PATH"
export UV_PYTHON_INSTALL_DIR="$ROOT/python"
export UV_CACHE_DIR="$ROOT/cache/uv"
export TMPDIR="$ROOT/tmp"
fetch "$BASE/cli/hub.pyz" "$WORK/hub.pyz"
fetch "$BASE/cli/hub.pyz.sha256" "$WORK/hub.pyz.sha256"
"$BIN/uv" run --managed-python --no-project --python 3.12 python -c '
import hashlib,pathlib,re,sys
p=pathlib.Path(sys.argv[1]); fields=p.with_suffix(".pyz.sha256").read_text().split()
if not fields or not re.fullmatch("[0-9a-fA-F]{64}", fields[0]): raise SystemExit("Invalid Hub checksum file")
if hashlib.sha256(p.read_bytes()).hexdigest()!=fields[0].lower(): raise SystemExit("Hub download checksum mismatch")
sys.path.insert(0,str(p))
from agent_hub_host.bootstrap import main
main(["--archive",str(p),"--base-url",sys.argv[2],"--uv",sys.argv[3]])
' "$WORK/hub.pyz" "$BASE" "$BIN/uv"
