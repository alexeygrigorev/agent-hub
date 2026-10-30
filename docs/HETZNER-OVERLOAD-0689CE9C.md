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

Heavy-test admission serializes registration and slot acquisition with
`admission.lock`. `fifo-state.json` contains the monotonic sequence counter and
only active waiters; a fixed `fifo-state.next` staging path is atomically renamed
under that mutex, so crashes expose either the previous or next complete index.
The counter and active set change together. Invalid state fails closed.

Each waiter holds a `flock` on a lease in `leases/`. A recorded but unlocked
lease belongs to a finished, timed-out or crashed waiter and is removed from the
active index under the mutex. Its lock **file is reused, never unlinked**. Lease
file count follows peak concurrent waiters rather than historical admissions.
Registration checks active leases once; subsequent retries check only the head
lease (discarding dead heads), and only the eligible head probes the slot files.
Retries sleep 1–2 seconds with jitter, bounded by the remaining wait budget.
There is no history-directory scan or per-retry subprocess. Existing slot and
admission lock files are never removed or replaced. The active-index file is
metadata, not a lock, and is replaced atomically.

FIFO is defined by registration sequence within a priority class. For the final
exact-source pre-push run use:

```bash
scripts/test-full-local.sh --priority final
# Or an individual command:
scripts/capped-test-run.sh --priority final -- COMMAND
```

Run the full runner directly: it acquires and releases a slot for each heavy
phase. Never put `test-full-local.sh` inside `capped-test-run.sh`: an outer
wrapper would hold a slot while its phases queue again. The full runner refuses
an inherited `AGENT_HUB_HEAVY_TEST_HELD=1` with exit 2 before starting any phase
and prints the direct invocation above. `--priority normal|final` sets the
priority for every phase; the existing priority environment variable also works.

`normal` is the default. `final` precedes normal waiters; reserve it for the last
exact-source pre-push verification, not routine development runs. The shared
host user can set this environment variable: it is scheduling policy, not an
authorization boundary. Admissions log the requested class and ticket number.
After 300 seconds a normal waiter joins the final class with its original
sequence, preventing later final arrivals from overtaking it indefinitely.
Priority and aging never preempt a running suite or increase the slot cap.
The existing wait budget still applies, including time before promotion.

Existing slot filenames remain compatible with older wrappers, but FIFO and
priority guarantees require all contenders to use the new wrapper. Old polling
or blocking contenders bypass the index and can win a free slot during rollout.
Update participating checkouts through their owners; do not stop or signal
other sessions to migrate them. Prototype tickets left by an earlier local
candidate are not read or deleted by the indexed scheduler.

TERM/INT sent only to the wrapper retain the running command's slot until it
exits. To stop an owned test run, signal its owning process group/session using
its normal lifecycle controls. An uncatchable wrapper-only crash releases its
descriptors, so commands that close inherited descriptors must also remain
inside their owning session's lifecycle containment. Supported Vitest/pytest
hooks use `AGENT_HUB_HEAVY_TEST_HELD=1` to avoid requesting a second slot.

Malformed, unknown-version or inaccessible admission state returns **exit 3**
with an `admission refused` diagnostic. Both supported direct-test hooks reject
that code, so a damaged index cannot start an uncapped suite. CLI usage mistakes
still return 2; command exit codes remain unchanged.

## Capacity-hold retry (intake 747d5bdf)

Exit 3 from `scripts/capped-test-run.sh` (no slot within the wait budget, or
admission refused) and `host_overload` from host dispatch are a HOLD, not a
code failure. Sessions must wait and retry with backoff instead of exiting:

```bash
scripts/capped-test-retry.sh -- ./my-heavy-step.sh
scripts/capped-test-retry.sh --attempts 8 --base-delay 60 --max-delay 600 -- scripts/test-full-local.sh
```

`scripts/test-full-local.sh` reports pure capacity holds as `HOLD` and exits
3, so the retry wrapper (or a manual re-run after load clears) can tell a
hold apart from a real failure. Direct `npx vitest` / `pytest` runs that hit
the in-process hooks (`heavy-test slot not acquired`) must be re-run under
`capped-test-run.sh` with the same wait budget, not abandoned.

A session must never exit with an uncommitted or unpushed fix without a Hub
note and an automatically queued follow-up:

```bash
scripts/require-clean-exit.sh --intake <id> --repo <path>
# if dirty:
hub intake show <id>  # read current version
echo '{"kind":"note","text":"CAPACITY HOLD — <state, output, next step>"}' \
  | hub intake append <id> --version <v> --key hold-<unique> --event-file -
echo '{"kind":"followup","text":"Retry heavy step after load clears; fix at <path/branch>"}' \
  | hub intake append <id> --version <v2> --key followup-<unique> --event-file -
# keep the worktree while the intake is open
```

The `followup` event keeps the intake in Hub's unresolved set for the
forgotten-work projection and restart queue, so Hub re-places the work on
the next cycle. Host dispatch already parks `host_overload` offers as
`held` (staying queued, retried next cycle) without touching running
sessions; the session-side retry above covers heavy steps inside an
already-running session.

For recovery, stop submitting new runs and notify the participating owners;
let waiters receive their refusal without signalling other sessions. Running
suites retain their slots. Under `admission.lock`, verify that every lease is
unlocked before repairing the index; if any waiter still holds one, release the
mutex and wait for its owner. Preserve the damaged metadata in the owner's
private evidence directory. Restore a verified compatible index atomically with
its monotonic `next` counter and no live waiter records; do not guess or reset
the counter. If no verified counter/state can be recovered, keep admission
refused until the queue owner supplies it. An unknown schema version requires
compatible wrappers, not overwriting another version's state. Never remove or
replace slot/admission/lease lock files, and never switch to a private lock root
to work around refusal. No automatic reset or cleanup occurs on errors.
