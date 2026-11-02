# Heavy tests and host load admission

Pre-push verification is the targeted `scripts/test-affected-local.sh` (docs/PREPUSH-TARGETED-TESTS.md); use `scripts/test-full-local.sh` only for broad changes and final gates. Its Vitest,
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

Each admitted command has a 1800-second wall-clock limit by default. Configure
it with `--run-timeout SECONDS` or `AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S` (1 to
86400 seconds). On expiry the wrapper sends TERM to the command's process group,
waits 10 seconds, then sends KILL; the slot stays held through that cleanup.
Timeout exits 124 and is a test failure, not a capacity HOLD. The run cap does
not change the shared slot count. Queue wait and command runtime are separate
budgets (`--wait` and `--run-timeout`).
Direct Vitest/pytest hooks watch their holder process and terminate the test
process if the holder reaches its cap, so the suite cannot continue after its
slot is released.

Host dispatch samples live host busyness before admission preparation and
again immediately before claim. A host is busy only on a **measured** signal
(Alexey t2358u-t2363u, 2026-11-02):

- CPU busy >= 98% averaged over >= 10s (`/proc/stat` deltas, iowait counted
  idle; snapshots persist in `~/.cache/agent-hub/host-load-cpu.json` so only
  the first sample after a gap sleeps, at most 10s);
- MemAvailable below 6 GiB (at most 25% of RAM on small hosts);
- a **runaway** 1-minute load above 6x cores (the 2026-10-28 incident was load
  275-295 on 12 cores, ~24x). This is a safety cap only: load average counts
  IO-wait and runnable threads, so 1.5x cores is routine (RMTHZ at load 24-37
  on 12 cores measured 35-70% CPU with 30 GiB free, and the old 1.5x gate held
  24 sessions queued for no reason). Load is never a reason below the cap.

When busy, the host does not start new work or touch running sessions. For an
unclaimed, non-task offer it refreshes the heartbeat and refuses that attempt
as `host_load_high` (heartbeat gates publish `host_overload`); Hub records the
attempt as not started and durably retains the refusing host and its factor on
the retry hold. Hub re-evaluates that retry live from the host's heartbeat:
fresh `cpu_usage.busy_percent_window` >= 98 or load above `max(stored factor,
6) x cores`. Legacy holds that stored factor 1.5 therefore release as soon as
the host is not actually busy. Fresh placement keeps its existing advisory-load
policy; launcher memory, quota and PSI/OOM gates still apply. Unreadable inputs
fail open. The optional local `host_load_factor` setting (0.5-8, default 6)
only moves the runaway cap; it is a valid `subscriptions.json` key (it was
missing from the validator until 2026-11-02, which made every host cycle fail
with "Invalid subscription configuration"). The host rechecks before every
claim as the final admission boundary.

A reserve refusal can also arrive while a prior claim response is still pending.
For `codex_quota_gate_codex_reserve_required` only, the host may abandon its
own persisted `claim_pending` attempt under the per-offer and journal locks.
It sends the existing attempt key; Hub matches the owning machine and key,
requires no native/task binding, retains the original as not started, and
creates a retry through ordinary quota/admission placement. A valid retry
receipt settles the local record. A different key in Hub's effect-free
starting offer supersedes only the old local pending record; it does not
mutate Hub's claim. Missing keys, native-start fences, uncertain records and
routed tasks never use this release path. Transport failures retain ownership
until an authenticated receipt is obtained. No claim keys appear in reports.

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
Waiting diagnostics include the current queue position and waiter count (for
example, `queue position 3/8`); this snapshot can change as normal tickets age
into the final class.
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
exits; the wrapper forwards shutdown to the command group and escalates after
the 10-second grace period. The bounded command run timeout applies the same
cleanup to hung commands and ordinary descendants. An uncatchable wrapper-only crash releases its
descriptors, so commands that close inherited descriptors must also remain
inside their owning session's lifecycle containment. Supported Vitest/pytest
hooks use `AGENT_HUB_HEAVY_TEST_HELD=1` to avoid requesting a second slot.

Malformed, unknown-version or inaccessible admission state returns **exit 3**
with an `admission refused` diagnostic. Both supported direct-test hooks reject
that code, so a damaged index cannot start an uncapped suite. CLI usage mistakes
still return 2; command exit codes remain unchanged.

## Capacity-hold retry (intakes 747d5bdf, 9432731e)

A capacity-gate refusal from `scripts/capped-test-run.sh` (no slot within the
wait budget, or admission refused) and `host_overload` from host dispatch are
a HOLD, not a code failure. Sessions wait and retry with backoff instead of
exiting:

```bash
scripts/capped-test-retry.sh -- ./my-heavy-step.sh
scripts/capped-test-retry.sh --attempts 8 --base-delay 60 --max-delay 600 -- scripts/test-full-local.sh --priority final
scripts/capped-test-retry.sh -- scripts/test-affected-local.sh
```

Exit 3 alone is ambiguous: the admitted command can exit 3 itself (pytest
INTERNALERROR). When `AGENT_HUB_HEAVY_TEST_REFUSAL_FILE` names a path, the
admission gate writes that marker only when it refuses before starting the
command, and removes the variable from the command's environment. A HOLD is
exit 3 plus the marker. A command's own exit 3 leaves no marker: the retry
wrapper reports it as FAIL (exit 1) and does not retry it.

`scripts/test-full-local.sh` gives each heavy phase its own marker. It reports a
refused phase as `HOLD` and a command's own exit 3 as `FAIL`. It exits 3 and
writes the caller's marker only when every failure was a hold; any real
failure makes it exit 1. The retry wrapper runs the full runner directly,
without an outer slot, because the runner takes a slot per heavy phase.
The affected lane takes one slot for its heavy block and prints the direct retry
command shown above on a capacity HOLD. Run it directly through
`capped-test-retry.sh`; never nest `test-affected-local.sh` inside
`capped-test-run.sh` or `heavy-test-admission.py`, or clear
`AGENT_HUB_HEAVY_TEST_HELD` in a child shell to bypass the nested-admission
guard. The retry command inherits `AGENT_HUB_HEAVY_TEST_PRIORITY` when set.
`--priority final` and the AGENTS.md push gates are unchanged. Direct
`npx vitest` / `pytest` runs that hit the in-process hooks (`heavy-test slot
not acquired`) must be rerun under `capped-test-retry.sh`, not abandoned.

A session must never exit with an uncommitted or unrecorded fix without a Hub
note and an automatically queued follow-up:

```bash
scripts/require-clean-exit.sh --intake <id> --repo <path>
# if it holds: commit on the agent branch, then
hub intake show <id>  # read current version
echo '{"kind":"report","reference":"<full sha>","source":"unknown"}' \
  | hub intake append <id> --version <v> --key commit-<unique> --event-file -
echo '{"kind":"note","text":"CAPACITY HOLD — <state, output, next step>"}' \
  | hub intake append <id> --version <v2> --key hold-<unique> --event-file -
echo '{"kind":"followup","text":"Retry heavy step after load clears; fix at <path/branch>"}' \
  | hub intake append <id> --version <v3> --key followup-<unique> --event-file -
# keep the worktree while the intake is open
```

The guard fetches `origin main` first. It reports the pushed state as unknown
and exits 1 when the fetch fails or `origin/main` is missing. Commits ahead
of `origin/main` count as clean only on a non-main agent branch whose HEAD is
recorded on the intake as a `commit` or `report` event with the full sha as
its reference. Worker credentials may append only `note`, `report` and
`blocker` events, so workers record the sha as `report` and queue the
follow-up as `blocker` instead of `followup`; both stay in Hub's unresolved
set. A clean result does not authorize
pushing to main: push only with the orchestrator-main release slot.

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
