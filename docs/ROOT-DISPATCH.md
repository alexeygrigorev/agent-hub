# Trusted Root dispatch (host agent, M4)

Operational root-run execution for the host agent. Scope of this document:
`host/agent_hub_host/root_dispatch.py`, the qualification in
`host/agent_hub_host/runtime.py`, the script-owned lifecycle in
`host/agent_hub_host/jobs.py` and the Root proof in `host/agent_hub_host/gate.py`.

**Status: not operational on this machine.** The host side is complete against the
boundary below, but the installed Quota Launcher has no Root boundary, the CAG script has
no Hub hooks and no pins are accepted, so qualification reports `UNSUPPORTED` with concrete
reasons and every root-run offer is declined before claim. Nothing here is runtime
evidence; the fixture tests simulate the *proposed, not yet accepted* script protocol.

## Ownership of the canonical root lock

The CAG script `scripts/root/headless-root-run.sh` **owns** `root.lock`, `state.json` and
the native epoch (healthy skip, stale takeover, heartbeat, fencing). The host never takes a
root lock, **not even a non-blocking probe**: a probe would make the timer's own `flock -n`
see a false busy holder. Lock state is read from `/proc/locks` (device + inode of
`root.lock`). `/proc/locks` omits a lock whose owner pid cannot be translated into the
reader's PID namespace (the script's lock helper process exits at once, so the recorded
owner is dead), so the read is trusted only from the **initial host PID namespace**
(`pid:[4026531836]` and a single `NSpid` entry); in any nested namespace a present lock
file reads `unknown` (HOLD), never "free". `GuardedRootBridge` stays read-only in
canonical mode.

Incumbent classification (`root_dispatch.canonical_incumbent_status`) returns
`none | healthy | starting | stale | crashed | unknown`:

| state | result |
| --- | --- |
| no `state.json`, lock free | `none`; lock held => `starting` |
| `state.json` corrupt / unreadable / not an object / `{}` / unknown `status` | `unknown` (HOLD) |
| lock state unreadable | `unknown` (HOLD) |
| status not `running`, lock held | `starting`; lock free => `none` |
| `running`, `pid_namespace` is not the host's | `unknown` (HOLD) |
| `running`, pid alive but its `/proc` identity is unreadable | `unknown` (HOLD) |
| `running`, pid dead or `pid_start_ticks` mismatch (pid reuse) | `crashed` (lock free) / `stale` |
| `running`, fresh heartbeat inside the 25 min window, pid alive | `healthy` |
| `running`, otherwise (or timestamps in the future) | `stale` |

`healthy`, `starting` and `unknown` decline the offer (`skip_healthy_incumbent` /
`root_state_unknown`) and are re-checked inside `adapter.begin`. `none`, `stale` and
`crashed` proceed: the script itself decides takeover.

## Dispatch path (script-owned lifecycle)

1. Offer received (`JobDispatcher.handle_offer`). `RuntimeManager.can_process_root_run()`
   requires: genuine qualification (below), the quota gate script, and a **live verified
   Hub scheduling read**. The incumbent must not be `healthy`/`starting`/`unknown`.
2. After claim, `_run_script_owned_root` runs: scheduling read (job credential) ->
   `begin_root_run` (adapter checks scheduling again, validates the entrypoint, runs the
   15 % quota gate, snapshots the incumbent, pre-assigns the run, refuses a unit name that
   is already in use, fetches fresh `quse`, launches) -> `wait_bootstrap` -> scheduling
   read -> Hub `start` -> start acknowledgement -> wait -> outcome -> Hub `finish`.
3. The host pre-assigns identities: run id `hub-<job>-e<lease epoch>`, launcher task id
   `hub-root-<job>-e<lease epoch>`, unit `agent-task-<task id>.service`. Pre-existing
   `runs/<run_id>.json`, `runs/<run_id>.hub/`, a `state.json` with that run id, or a live
   unit of that name refuse the dispatch with zero effects.
4. Failures, a lost lease, a deadline, a Hub `start` refusal or a scheduling denial before
   start **cancel and wait only the exact owned unit** before the Hub is told. Cancel
   reads the live `InvocationID`, requires it to equal the validated receipt's
   invocation (or, before a receipt, the first invocation seen on the exact owned unit
   with an active state and the unit in its cgroup) and only then issues `systemctl --user
   stop` (then `kill`) for that unit. A foreign or replaced unit is never signalled.
   `cancel_verified=True` needs the launcher thread ended **and** a live `systemctl show`
   reporting the owned unit `not-found`/`inactive`/`failed` or its `InvocationID` changed;
   thread exit alone never counts. If it cannot be confirmed the Hub is told
   `cancel_verified=False`.
5. A launcher that raises after Hub `start`, or ends without a verified clean exit,
   may leave the owned unit live. A production cleanup receipt must name the expected unit,
   carry an integer exit code (not a boolean), set `cleanup_verified` to `true`, and carry
   a 32-digit hexadecimal `invocation_id` matching the owned invocation when known.
   Missing, malformed or foreign fields cannot prove cleanup: the host cancels
   and live-verifies that exact invocation (as in step 4) **before** it reports the outcome,
   which is `failed` and carries `cancel_verified=<result>`.

## Script protocol the host requires (proposed; pending CAG pin)

Private per-job environment (0600 env file written by the launcher; never argv, never
`-E`): `HUB_JOB_ID`, `HUB_URL`, `HUB_LEASE_EPOCH`, `HUB_JOB_TOKEN`, `HUB_HOST_ID`,
`HUB_LAUNCHER_TASK_ID`, `HUB_LAUNCHER_UNIT`, `HUB_EXPECTED_RUN_ID`, `HUB_FIRST_ACTION_FILE`,
`HUB_START_ACK_FILE`, and `HR_STATE` (same canonical dir as the host reads).

The script: takes the same `root.lock`; uses `HUB_EXPECTED_RUN_ID` as its run id; writes
`state.json` with `run_id, pid, pgid, pid_namespace, pid_start_ticks, epoch, hub_job_id,
hub_lease_epoch, launcher_task_id, launcher_unit, launcher_invocation_id (systemd
$INVOCATION_ID), start, heartbeat, status` **before** the receipt; writes the receipt
atomically to `HUB_FIRST_ACTION_FILE`; **waits for `HUB_START_ACK_FILE`** before any
business step; always writes `runs/<run_id>.json` with `status` in
`ok | skipped | quota_held | timeout | error` (a skip is `skipped`).

Receipt (`schema_version: 1`): `kind` (`observed_root_bootstrap` | `observed_native_tool`),
`job_id, host_id, hub_lease_epoch, native_epoch, run_id, launcher_task_id, launcher_unit,
launcher_invocation_id, observed_at_utc` (timezone-aware), `actual_action,
actual_action_digest` (sha256), `native_session_id` (null for bootstrap; required for a
native tool), `source_entrypoint_sha256`.

The host accepts a receipt only if **all** of these hold: every identity field equals the
host's own expectation; `source_entrypoint_sha256` equals the hash the host computed from
the validated entrypoint; `observed_at_utc` and file mtime fall inside the dispatch window;
`state.json` is this run, `running`, and carries the same job, lease epoch, native epoch,
task, unit and invocation; `start` is not older than the dispatch; the recorded pid is not
the host or its parent; `pid_namespace` equals the host's PID namespace (trusted host
namespace; `pid:[1]` or a missing value is refused); the live `/proc/<pid>` start ticks
equal `pid_start_ticks`; the process cgroup contains the owned unit; the live systemd
`InvocationID` of the unit equals the receipt's. Files must be regular, same-owner, not
symlinks, not world-writable and under 64 KiB. Anything else is zero evidence.

The **bootstrap** receipt is the script's own first action. It is *not* the model's first
tool call: that is a separate `observed_native_tool` receipt written later to the same
file, and until it is observed the summary says `native first tool: UNKNOWN`. Neither is
ever derived from state or a PID.

The **start acknowledgement** (`start-ack.json`, 0600, atomic) is written only after a
validated bootstrap receipt and a Hub `start` answer without error. It binds job, host,
lease epoch, native epoch, run, task, unit, invocation and the receipt digest; it contains
no token.

## Outcome (B1)

The Hub status comes from the run's own `runs/<run_id>.json`, never from the exit code
alone (the script always exits 0) and never from a "fence" inference:

| run summary | Hub status |
| --- | --- |
| `ok` with a validated receipt, the ack written, exit 0 | `succeeded` |
| `skipped`, `quota_held` (no Root cycle ran) | `cancelled` (never a successful cycle; no Hub `start`) |
| `timeout`, `error` | `failed` |
| no summary, or `state.json` still `running` this run after the unit exited | `failed` (crashed) |
| `state.json` now owned by another run | `failed` (fenced) |
| `ok` without receipt/ack, or a summary for a different run | `failed` |

Launcher receipts need `unit_name == the owned unit`, an integer `exit_code`, and for a
real (non-fixture) executor `cleanup_verified is True` and a non-empty `invocation_id`.
A `MainPID` is never accepted as a unit identity.

## Scheduling (B6)

`HubSchedulingReader` does a **live** `GET <paired hub>/api/root/scheduling` with the
paired host token (or the job token) for every new Root run: at offer, before dispatch,
and before Hub `start`. Same origin only (`api.check_url`, no redirects). The answer must
be an object with a boolean `enabled` and a timezone-aware `server_time` within 120 s of
local time and not behind a previously accepted `server_time`; `updated_at` may be null or
not later than `server_time`. Missing credential, unreachable, 401/403, 404, other HTTP
errors, stale, regressed or corrupt answers all **deny**. Running Roots keep renewing and
finishing when scheduling is disabled; nothing after Hub `start` consults the flag.

The verified answer is recorded in the host cache dir (`~/.local/state/agent-hub-host/
scheduling.json`, 0700 dir, 0600 file, never under `HR_STATE`; configuring the two into
one tree is refused). The cache is **not authority**: a local file can only add a denial
reason when the Hub is unreachable and can never grant dispatch.

**Server boundary:** `GET /api/root/scheduling` accepts an admin session or the existing
paired host bearer credential with host scope and kind, subject to revocation and expiry
checks. It returns `{"enabled": bool, "updated_at": iso|null, "server_time": iso-utc-now}`.
A supplied job credential is tried first by the reader; on 401/403 it falls back to the
paired host credential. The host does not self-grant admin; changing the flag with `PUT`
requires an admin session.

## Qualification (B3, B4)

`LauncherQualification.inspect_interfaces()` reports `SUPPORTED` only when all verify:
launcher commit in `root_dispatch.ACCEPTED_LAUNCHER_COMMITS`; `root-run` in
`launcher.task_profiles.TASK_PROFILES`; a `codex` or `root-run` route in
`launcher.admission.ADAPTER_ROUTES`; `execute_transient_task_unit` *names* (AST of the
source; a `**kwargs` catch-all or docstring text does not count) `task_id, command_argv,
memory_mb, workspace, tmpdir, timeout_sec, quse_json, provider, check_capacity, profile`
and `job_env` or `job_env_file`; the CAG entrypoint validates and its sha256 is in
`ACCEPTED_CAG_SCRIPT_SHA256`; the quota gate is readable. Both pin sets are committed
constants and are **empty** until the head accepts the Quota Launcher Root profile and the
CAG hooks. `launcher.*` is imported from exactly the inspected checkout (cached modules
of another checkout cannot answer for it). The trusted host PID namespace is additionally
verified per run from the receipt, `state.json` and live `/proc`.

The entrypoint is `TrustedRootConfig`: `~/` is expanded, then the path must be absolute,
free of `..` and of symlinks in any component, a regular executable file owned by this
user or root, not world-writable, under the CAG repo root. Qualification and adapter share
one config object. Injected executors are test seams: they mark receipts `fixture_only`
and can never make `can_process_root_run` operational. So is every other injected seam
(`proc_probe`, `invocation_probe`, `host_namespace`, `unit_controller`, `scheduling_check`,
`quota_check`, `quse_provider`); production passes none. `launcher.*` imports share one lock
and a cached module that was not loaded from the inspected launcher checkout is refused.

`LauncherExecutor` (the default executor) calls the installed `execute_transient_task_unit`
with the private job env (`job_env`, or a 0600 `job_env_file` it removes afterwards), the
fresh `quse_json`, `provider="codex"`, `workspace`, `tmpdir` under
`<workspace>/.local/tmp/<task>`, `check_capacity=True` (resource/capacity floors),
`timeout_sec` and `profile="root-run"`. If the installed signature does not name all of
them it raises `launcher_root_boundary_unsupported` and starts nothing; today it does.

`gate.py` keeps `pending#59` (`launch_allowed=false`) unless
`runtime.operational_root_profile_proof()` — the same qualification, no caller input — says
`SUPPORTED`. A missing launcher SDK or any exception denies.

## Needed from other owners

- **Quota Launcher:** `root-run` profile and route; explicit `profile` + `job_env` (or
  `job_env_file`) on `execute_transient_task_unit`; a trusted host PID namespace with
  `ReadWritePaths` for the Root state dir (no private PID namespace, no read-only state);
  the host agent itself must run in the initial PID namespace to read `root.lock`;
  receipt with `unit_name, invocation_id, exit_code, cleanup_verified`.
- **CAG script:** the protocol above, including the skipped summary.
- **Hub server:** the scoped host/job read of `GET /api/root/scheduling` above.
- **Head:** accepted commit/sha256 pins added to `root_dispatch.ACCEPTED_*`, then two
  genuine Hub Root cycles (with Root output, lease/run records and the native first-tool
  receipt) before the timer is retired.

## Test posture

- Fixture-only positives are explicit: `FakeRootEnv` simulates the proposed script
  protocol with a real stand-in process in a temp dir; receipts are `fixture_only`.
- Negatives cover: tampered/foreign/stale receipts and state bindings, no-op and foreign
  incumbents, skip/crash/error/timeout outcomes, ack ordering, exact-unit cancel (including
  a replaced unit, a unit that never appears, and an executor exception that leaves the unit
  live), executor token scrubbing, the non-init PID namespace lock guard (a real `flock`
  helper holding a private temp lock), scheduling
  deny cases and a real local HTTP transport, pins, missing/`**kwargs` launcher API, the
  real default entrypoint, corrupt/foreign/ambiguous incumbent states, the absence of any
  flock use, and the gate proof.
