#!/usr/bin/env bash
set -euo pipefail

for attempt in $(seq 1 20); do
  echo "session-messaging-m1 repetition ${attempt}/20"
  npx --no-install vitest run test/session-messaging-m1.test.ts \
    --configLoader runner --no-file-parallelism --maxWorkers 1 --disableConsoleIntercept
done
