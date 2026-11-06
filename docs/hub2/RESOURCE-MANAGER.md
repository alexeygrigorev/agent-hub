# Hub 2.0 Resource Manager and native execution

Status: implementation in progress, 5 November 2026. This document records the
Alexey-authorized redesign that makes the Hub own quota and direct execution
as one coherent Resource Manager component inside Hub2, replacing the external
Agent Quota Launcher dependency on the launch path. Alexey's controlling
direction (2026-11-05, verbatim in the coordination ledger): "we have our quota
that lives in hub. I want hab to collect the quota and keep it in the db, and
based on that launch sessions. no need for a separate service"; "also wheres
the resource manager? quota is a part of that"; "lets have a separate concrete
block resource manager".

## Ownership diagram

```
                        ┌────────────────────────────────────────────────┐
                        │                    Hub2 Worker                 │
                        │  API / CLI / MCP / dashboard front door, auth  │
                        └───────┬─────────────────────────────┬──────────┘
                                │                             │
              ┌─────────────────▼──────────┐      ┌───────────▼───────────┐
              │       Task Manager         │      │    Cost Controller    │
              │ (ProjectDO: asks, AC,      │      │ (BudgetDO + edge:     │
              │  queue, deps, schedules,   │      │  USD 0.03/day alert,  │
              │  dispatch saga = durable   │      │  enforced ceiling;    │
              │  launch intent)            │      │  separate currency)   │
              └───────┬────────────────────┘      └───────────┬───────────┘
                      │ 1. request admission                  │ gates fanout
                      ▼                                       ▼ before effects
              ┌───────────────────────────────────────────────────────┐
              │                Resource Manager (this)                │
              │  hub2/src/resource-manager.ts — one admission facade: │
              │  availability + freshness classification, typed holds │
              │  (unknown/stale/exhausted quota, stale host snapshot, │
              │  capability/machine unavailability), account/model/   │
              │  machine selection policy, structured rejections      │
              └───┬───────────────────────────┬───────────────────────┘
                  │ consumes persisted state  │ 2. returns admitted plan
                  │ (never probes providers)  │    or typed hold
      ┌───────────▼────────────┐   ┌──────────▼────────────────┐
      │  AccountDO (existing)  │   │  MachineDO (existing)     │
      │  quota/reset/credential│   │  engines, capabilities,   │
      │  health snapshots,     │   │  host resource snapshot   │
      │  reservations          │   │  (cpu/mem/slots), slots,  │
      │  (authoritative)       │   │  offers, deadlines        │
      └───────────▲────────────┘   └──────────▲────────────────┘
                  │ 3. reserve (idempotent,   │ 3. reserve slot (CAS,
                  │    epoch-fenced)          │    epoch-fenced)
      ┌───────────┴───────────────────────────┴────────────────────┐
      │  Host Agent (hub2/host/agent_hub_host)                     │
      │  4. receives admitted launch plan                          │
      │  5. rechecks physical resources (TOCTOU guard)             │
      │  6. final provider-generation gate vs live admission       │
      │  7. private provider home + scoped child context           │
      │  8. spawns DIRECTLY via aplexer (process supervisor)       │
      │  9. observes/stops actual process; qualified exit,         │
      │     deadline and containment proof from the tombstone      │
      └────────────────────────────────────────────────────────────┘
                      │ durable outbox (started/finish receipts)
                      ▼
              Message Bus / MailboxDO (separate durable communication)
```

Boundaries that must not merge:

- **Resource Manager is not a service.** It is a module inside the Hub2
  Worker with no SQLite of its own, no Durable Object, no polling loop, and
  no network hop. All state it reads is already persisted by AccountDO and
  MachineDO; all reservations it requests stay owned by those DOs.
- **Quota is one resource input.** The Hub-owned collector (host-reported
  quota evidence, `PUT /api/account/quota` with `quota:write`, generation-
  fenced) is the only quota source. Unknown or stale quota becomes a typed
  hold with a bounded refresh trigger, never fake zero and never a local
  `quse` probe. Automatic reset redemption stays prohibited.
- **Task Manager owns intent; the Resource Manager coordinates; owners own
  capacity.** The ProjectDO dispatch saga remains the single durable
  launch-intent owner with compensation: it journals each step, schedules
  retries, and compensates partial holds. The RM facade
  (`advanceDispatch`/`releaseAccountCapacity`) orchestrates machine slot
  reserve/activate and account reserve/confirm/release by calling the
  existing authoritative MachineDO/AccountDO APIs with the same idempotent,
  epoch-fenced payloads as before — no new DO, no durable state outside the
  owners, no fake cross-DO atomic transaction, and no release of uncertain
  native occupancy (the qualified-proof gate stays in the account owner).
- **Cost Controller is a separate monetary guard.** BudgetDO/edge consults
  happen before expensive fanout (dispatch already wraps RM selection in
  `budgetedBackgroundRequest`). Provider quota percent and infra USD are
  never merged into one counter.
- **The Message Bus is separate durable communication.** MailboxDO outbox/
  inbox and delivery receipts are untouched by this redesign.
- **aplexer stays the process supervisor.** The Hub never implements a
  terminal or a PTY; the host spawns through `aplexer start` and observes
  native state, tombstones, exit and containment.

## Old -> new responsibility and import mapping

| Old (external) | Old location | New | Notes |
| --- | --- | --- | --- |
| Advisory route selection (`selectRoute`) | `hub2/src/routing.ts` | Replaced by `admitLaunch` in `hub2/src/resource-manager.ts` | Adds typed holds (`quota_unknown`, `quota_stale`, `quota_exhausted`, `account_saturated`, `host_resources_stale`) instead of a single `quota_unavailable` refusal; wire error codes stay compatible |
| External launcher CLI (`launcher submit/run/status/reconcile`, admission, ranking, store, task profiles, resource policy) | `agent-quota-launcher` package (runtime + PYTHONPATH repository in host config) | Removed from the Hub2 launch path; admission authority moved to the Hub RM, host spawn moved to `hub2/host/agent_hub_host/native_launch.py` | The generic framework (ranking, task units, resource-policy store, watch loop) is NOT copied; the external repo stays intact for its other consumers |
| `launcher/provider_home.py` materialization + verification | external package, invoked as `python -m launcher.provider_home` with PYTHONPATH | `hub2/host/agent_hub_host/provider_home.py` (in-process) | Behavior-preserving port: same bundle schema, access-only stripping, generation fencing, harness files, manifest fingerprints, private-file checks; Windows ACL verification included |
| `provider_home.admission` final pre-spawn gate | external, called at spawn with controller context | `provider_admission()` in the internalized module, reusing the host's authenticated client | Same checks: binding identity, generation, quota freshness window, runtime deadline, codex floor (codex-family only) |
| Managed-profile spawn wrapper (`python -c provisioning.execute <label> ...` into the Hub1 host package) | launcher `build_managed_profile_argv` + Hub1 `agent_hub_host/provisioning.py` | Direct engine argv from `native_launch.adapter_argv` with the AccountDO-materialized private home environment | Removes both the launcher AND the Hub1 provisioning runtime dependency from the launch path; no installer/quota-gate logic is copied (quota is a Hub RM decision) |
| Interactive/effort/goal argv construction (`interactive.py`) | external | Ported into `native_launch.py` (`interactive_argv`, `headless_effort_argv`, `goal_prompt`, `goal_store`, goal registration) | Same shapes and refusal slugs; goal registration keeps the exact-thread proof |
| First-action artifact validation | external `launch.validate_first_action` | Ported into `native_launch.py` | Identity match against the start record + live-mutation + clone rejection |
| Tombstone retirement proof (`native_lifecycle.read_tombstone/verify_tombstone`) | external | Ported into `native_launch.py` (`read_tombstone`, `verify_tombstone`, `NativeLauncher.retired/finalize`) | Bounded envelope (1 MiB) and metadata (64 KiB) caps, symlink/nonregular rejection, UUID canonicalization preserved |
| Launcher capabilities manifest (hash-pinned external files) | external `capabilities_manifest` | `NativeLauncher.capabilities()` from the host route table + provider binary presence + provider support matrix | Hub-side engine capability advertisement; the machine capability snapshot in MachineDO stays the Hub-visible truth |
| Host local admission (quse-based quota gate, launcher store reserves, CPU choking policy) | external launch critical section | Removed from the host. Physical TOCTOU recheck only: `MemAvailable >= min_free_mem_mb + requested` before spawn (`host_physical_resources_insufficient`), typed host error to the saga | Quota and capacity policy are Hub-side; the host never invents resource availability |
| Host adapter `Launcher` class | `hub2/host/agent_hub_host/launcher.py` | `NativeLauncher` in `native_launch.py` | Same host role: launch receipt verification, recovery, aborted identity, retirement; the removed adapter is not a Hub2 runtime fallback |

## Host launch contract (post-redesign)

Host config gains a `native_launch` block next to the legacy `launcher` one:

```json
{
  "native_launch": {
    "routes": {
      "codex": {"provider": "zai", "account_profile": "hub-zai-central"},
      "opencode": {"provider": "go"}
    },
    "aplexer": "a",
    "aplexer_state_dir": "~/.local/state/aplexer",
    "memory_mb": 1500,
    "pids": 256,
    "min_free_mem_mb": 6000
  }
}
```

- `routes` keeps the trusted-installation shape (provider + optional account
  profile); the exact model always comes from the Hub-admitted plan, never
  from task text. GLM models are refused on any route other than `zai`, and
  OpenCode is pinned to its approved subscription model.
- Central-home launches require `client`, `session_id`, `operation_id` and
  `epoch` on the launch specification so the final gate re-verifies the exact
  admitted generation against `/api/sessions/:id/admission` with
  `provider_check: true` immediately before spawn. The old per-session
  controller config file is gone with the external launcher path: no
  parent-token file is written next to workspaces anymore.
- The child runs with the materialized private home environment and a scoped
  child Hub context (`HUB2_CONFIG`), never with the host's operator token;
  no tokens enter argv, env of the host process, or logs.
- First action is proven by the child's `aplexer whoami` artifact checked
  against the launch start record (identity + live mutation + clone
  rejection). Exit is proven by the aplexer tombstone (identity, exit code,
  `containment_empty`), which is also what releases reservations — the saga
  compensates only after qualified exit or a confirmed stop, and an
  uncertain launch never re-spawns.

## Collectors (no double authority, no polling service)

- Provider quota/reset/credential health: collected by the Hub2 host
  provider path from the account's own SDK state and reported as quota
  evidence into AccountDO (`PUT /api/account/quota`, bounded freshness,
  generation-stamped source). AccountDO's lazy refresh and discovery
  deadlines continue to own staleness; the RM only classifies freshness and
  triggers holds. Collection runs on stale placement/view and scheduled
  discovery, never as a hot global poll.
- Host CPU/memory/slots/engines/capabilities: the host heartbeat advertises
  engines and capabilities (existing) plus a bounded resource snapshot
  `{mem_available_mb, observed_at}`. MachineDO persists it in
  `machine_capabilities.resources_json` and exposes `resources` verbatim in
  its `/_internal/routing` view — that wire shape is the admission contract.
  Classification fails closed: a missing or non-positive snapshot is a
  `host_resources_unknown` hold, a snapshot older than 300 s is
  `host_resources_stale`; only a fresh positive snapshot admits. Hosts refresh
  every 240 s, leaving at least a 60 s timing margin before scheduled starts.
  The host also rechecks physical
  resources immediately before every spawn. Cutover note: hosts must run a
  heartbeat that includes the snapshot before admission passes.

## Acceptance coverage

`hub2/tests/resource-manager.test.ts` and `hub2/host/tests/test_native_launch.py`
cover: fresh-quota selection determinism and weighting; unknown/stale/
exhausted/saturated quota typed holds (reset-time retry for exhausted);
stale/absent host resource snapshots; goal/override/capability preflights;
provider route argv (zai harness flags, pinned OpenCode model, GLM routing
refusal); effort/interactive/goal argv transforms; first-action artifact
validation including clone rejection; tombstone bounded reads, identity and
containment refusals; provider home materialization idempotence, privacy,
schema refusals; provider admission binding/generation/freshness enforcement;
physical-resource fail-closed behavior; launch refusal on unknown routes and
insufficient memory.

The native integration canary (real `aplexer start` spawn through the new
adapter against the live Worker) is a release gate. Until it passes on the
exact integrated head, production remains in maintenance mode. The external
launcher repository and its history remain available to other consumers, but
Hub2 has no external-adapter runtime fallback. The legacy `hub1` tag preserves
the old source revision; it does not restore the Hub namespace or deleted
database after the authorized clean start.
