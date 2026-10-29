# Heavy tests and host load admission

Use `scripts/test-full-local.sh` for the full verification gate. Its Vitest,
Python, host-canary, and standalone browser phases acquire a host-wide heavy-test slot.
For ad-hoc suites use:

```bash
scripts/capped-test-run.sh -- npx vitest run
scripts/capped-test-run.sh -- uv run --with pytest python -m pytest
scripts/capped-test-run.sh -- node scripts/test-browser-local.mjs
```

All checkouts under the same host user share two slots by default. The Linux
`flock` locks live under `/run/user/<uid>/agent-hub/heavy-test`. The supervising
wrapper retains its lock until the command finishes, even if a runtime closes
inherited descriptors. Kernel locks release automatically when holders exit.
Never unlink the lock files while suites might be running. A contender waits up
to 1800 seconds, then exits 3 without signalling any holder. Command exit codes
pass through. The full runner captures the shared directory before switching
Python to a disposable HOME and removing live credential selectors.

`AGENT_HUB_HEAVY_TEST_SLOTS`, `AGENT_HUB_HEAVY_TEST_WAIT_S`, and
`AGENT_HUB_HEAVY_TEST_DIR` configure slots, wait budget, and lock location.
Use the same slot count and directory across participating sessions; a private
lock directory is appropriate only for isolated wrapper tests.

Host dispatch samples the live one-minute load before admission preparation
and again immediately before claim. Above 1.5 times the core count, offers are
held for retry without rejecting them or touching running sessions. This counts
load from subprocesses that start inside already admitted sessions, including
Vitest workerd pools and Chromium. Existing launcher memory, quota and pressure
gates still apply. Unreadable load inputs report unknown and preserve existing
admission behavior. The optional local `host_load_factor` setting overrides the
default within 0.5–8. Heartbeat gates publish overload as `host_overload`; server
placement still treats the aggregate gate as advisory, so dispatch enforces the
live check on every execution path.

The fleet-host oneshot template uses `TimeoutStartSec=600`. It performs sequential
provisioning, quota, catalog, reconciliation and dispatch work, whose combined
latency can exceed the previous 240-second budget under load. Install the timeout
without replacing existing release/configuration drop-ins, then reload systemd;
let an active cycle finish. Use the verified host-release builder and switcher
for code rollout, retaining existing config paths. Do not stop native work sessions.

Acceptance evidence belongs on intake `0689ce9c`: the baseline process ownership,
load and timeout counts; targeted test output (the active intake continuation
explicitly forbids full suites on the loaded host); exact integrated commit and deployment
run; post-rollout command acknowledgements; and at least two hours of journal
coverage with zero startup timeouts. A successful build or a longer timeout alone
does not establish live acceptance.
