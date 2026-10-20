#!/usr/bin/env bash
# Local-only Worker + clean machine container. No production credentials/config mounted.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .local/tmp
export TMPDIR="$PWD/.local/tmp"
TEST_DIR=$(mktemp -d "$TMPDIR/engine-docker-XXXXXX")
WRANGLER_PID=''
CONTAINER="hub-engines-test-$$"
cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  if [ -n "$WRANGLER_PID" ]; then kill "$WRANGLER_PID" 2>/dev/null || true; wait "$WRANGLER_PID" 2>/dev/null || true; fi
  rm -rf "$TEST_DIR"
}
trap cleanup EXIT
APLEXER_BINARY="${APLEXER_BINARY:-$(command -v aplexer)}"
cp "$APLEXER_BINARY" "$TEST_DIR/aplexer"
cp test/docker/provisioning.Dockerfile "$TEST_DIR/Dockerfile"
cp test/docker/lifecycle.py "$TEST_DIR/lifecycle.py"
python3 scripts/build-hub-cli.py --out-dir public/cli >/dev/null
# Exclusive fixed loopback test port; fail rather than using an unrelated server.
python3 -c 'import socket; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1",19487)); s.close()'
node node_modules/wrangler/wrangler-dist/cli.js dev --config test/wrangler.provisioning-docker.jsonc --ip 127.0.0.1 --port 19487 --persist-to "$TEST_DIR/state" >"$TEST_DIR/worker.log" 2>&1 &
WRANGLER_PID=$!
for attempt in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:19487/cli/install.sh >/dev/null 2>&1; then break; fi
  if ! kill -0 "$WRANGLER_PID" 2>/dev/null; then cat "$TEST_DIR/worker.log"; exit 1; fi
  sleep 1
done
DOCKER_BUILDKIT=${DOCKER_BUILDKIT:-0} docker build --memory 1500m -t hub-engine-lifecycle-test "$TEST_DIR"
docker run --rm --name "$CONTAINER" --network host --memory 1500m --pids-limit 256 ${TEST_PROVIDERS:+-e TEST_PROVIDERS="$TEST_PROVIDERS"} hub-engine-lifecycle-test
