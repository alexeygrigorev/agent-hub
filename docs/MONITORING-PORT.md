> Runtime coverage audit, 17 October 2026, 21:54 Berlin: this port is **partial**. Real Hetzner disk and memory/load collection, API/status/UI, alert/task creation and exact Git/CI/Cloudflare probes are verified. Only the default liveness scheduled check is currently enabled; installed check kinds are not proof of active monitoring. Existing competition timers/services remain in place. Source or policy alone does not supersede their monitoring. The corrected rows below identify the missing activation or implementation work.

# Monitoring Port Plan & Superseded Checks (Addendum 6b)

- **Canonical Parent Hub Task**: `75e79822-6996-484f-8aee-194d0be39d10`
- **Scope**: Porting plan from `cloudflare-agent-git` monitoring inventory to Agent Hub
- **Author**: `platform-monitor-inventory`
- **Reference Document**: [docs/MONITORING-INVENTORY.md](MONITORING-INVENTORY.md)

---

## 1. Executive Summary

This document establishes the concrete, prioritized porting plan for transitioning monitoring signals from the historical Cloudflare competition codebase (`cloudflare-agent-git`) into Agent Hub (`agent-hub`).

Following Alexey's Addendum 6b instructions, the goal is to port all useful signals into Hub's host collector, status projections, and alert mechanisms before competition directories undergo disk cleanup. At the same time, this document explicitly marks superseded, obsolete, or discarded checks (such as legacy cross-model review gates and standalone supervisor daemons) so they are **not** accidentally re-introduced or re-enabled.

---

## 2. Migration decisions and retained checks

Do not add duplicate supervisors. Retain existing monitors until the Hub observes the same targets and an actual replacement/handoff is verified. A proposed architecture does not establish supersession.

| Legacy component | Source | Current decision | Hub seam or remaining work |
| :--- | :--- | :--- | :--- |
| Cross-model review gate | `scripts/guards/review_gate.py` | Alexey removed this shipping gate on17October. | Focused tested main commits; no new review freeze. |
| Autonomous supervision daemon | `scripts/supervision/service.py` | Retain current service during migration; do not launch a duplicate. | Hub checks and host observers cover some signals; full outcome/recovery coverage remains incomplete. |
| Supervisor watchdog timers | `supervision-watcher-1.timer`, `supervision_watcher.sh` | Retain; target-specific replacement is unproved. | Hub controller heartbeat monitors its controller, not the legacy supervisor status file. |
| Desktop mailbox watchdog | `desktop-orchestrator-watchdog.sh`, `.timer` | Retain; no equivalent live target observation is proved. | Add read-only observation before proposing a retirement/handoff. |
| Standalone principal ping | `scripts/ping/principal-ping.py`, `.timer` | Retain; M8b principal-wake is under implementation. | Source leases and cron do not prove actual consecutive-miss recovery. |
| FileBus pre-commit claims | `scripts/checks/claims.sh`, `scripts/guards/claims_check.py` | Do not recreate obsolete commit blockers. | Retain development edit scopes and separate Hub runtime lease fencing. |
| Machine-status Git linter | `scripts/checks/machine-status.sh` | No runtime monitoring value; do not port the markdown gate. | Use actual enrolled-host observations and explicit expected-offline state. |
| Strict commit-message linter | `scripts/checks/commit-message.sh` | Do not port a shipping gate. | Concise main commits under current Alexey instructions. |

---

## 3. Prioritized Concrete Port Checklist

### Priority 0: Immediate Critical Reliability & Resource Safety

These signals directly prevent service outages, disk exhaustion, runaway token expenditure, and unobserved process deaths.

1. **Filesystem Disk Usage & Threshold Alerting (Addendum 6)**
   - **Target Seams**:
     - Host Collector: `host/agent_hub_host/disk_monitor.py`
     - Host Hook: `host/agent_hub_host/session_host.py`
     - Hub Durable Object: `src/monitoring.ts`, `src/status.ts`, `src/hub.ts`
     - Dashboard UI: `public/fleet.html`, `public/fleet.js`, `public/fleet.css`
     - Docs: `docs/DISK-MONITORING.md` (peer `platform-disk-monitor` stream)
   - **Requirements**:
     - Enumerate filesystems via Linux `statvfs` stdlib across all mounts: `/`, `/home`, `/data`, `/tmp`, and bind mounts.
     - Report `mount`, `size`, `used`, `free`, `percent` with stable machine identity.
     - Thresholds: Warning at $\ge 85\%$, Critical at $\ge 95\%$.
     - Top consumers: When threshold is crossed, execute bounded recursive directory scan (max depth, max entries, time limit) on trusted roots.
     - Alerting: Raise deduplicated Hub alert/task per host/filesystem episode; recover when usage drops below threshold.

2. **Freshness Watchdogs for Collectors & Metrics**
   - **Target Seam**: `src/checks.ts` (`Checks.evaluate()`) + host `session_host.py`
   - **Requirements**:
     - Evaluate timestamp of latest host status report and metrics snapshot.
     - If age exceeds 600s (10 min), mark host state as `stale` or `degraded` and raise alert.
     - If age exceeds 900s (15 min), mark host as `unavailable` per SPEC.md §9.

3. **Resting State Contradiction & Worker Stalls**
   - **Target Seam**: `src/session_lifecycle.ts` (`classifySession()`)
   - **Requirements**:
     - Port logic from `check_resting_state_contradicted`: detect when `last_activity_ms > reported_state_at_ms + 1000`.
     - Flag sessions that report `idle` or `waiting` but have active PTY output.
     - Enforce request delivery SLO (300s standard limit); flag undelivered / unacknowledged requests.

4. **Quota Reserve Gate Enforcement**
   - **Target Seam**: `src/checks.ts` (`kind: 'quota'`), `host/agent_hub_host/quota.py`
   - **Requirements**:
     - Check provider remaining quota percentage before scheduling or launching sessions.
     - Maintain strict fail-closed 15% reserve threshold (`percent_remaining > 15`).

5. **Single-Instance Root Run & Task Leases**
   - **Target Seam**: Hub `leases` table + `host/agent_hub_host/guarded_root.py`
   - **Requirements**:
     - Atomic lease acquisition with generation/epoch fencing.
     - Detect stale heartbeat (> 180s) and allow safe succession while fencing previous runs.

---

### Priority 1: Operational Observability & Host Health

These signals provide operational clarity into host workload, execution progress, and deployment state.

1. **Host Resource Utilization (Load Average & Memory)**
   - **Target Seam**: `host/agent_hub_host/status_collect.py`, `/api/metrics`
   - **Requirements**:
     - Collect `os.getloadavg()` (1m, 5m, 15m).
     - Collect host memory statistics (`MemAvailable`, total memory).
     - Surface in Hub host status and fleet dashboard cards.

2. **Process Health & Start-Ticks Anti-Reuse**
   - **Target Seam**: `host/agent_hub_host/native_sessions.py`
   - **Requirements**:
     - Read `/proc/{pid}/stat` start ticks (`start_ticks`) to prevent forwarding signals to recycled PIDs.
     - Detect zombie processes (`Z` state).
     - Require genuine `aplexer whoami --json` validation before transitioning session to running.

3. **Message Bus Inbox Backlog Monitoring**
   - **Target Seam**: `src/checks.ts` (`inbox_backlog`)
   - **Requirements**:
     - Query unacknowledged messages for each recipient actor.
     - Alert when unacknowledged message age exceeds configured threshold (`max_age_s`).

4. **Source, Deployed & CI Drift Probes**
   - **Target Seam**: `host/agent_hub_host/status_probe.py` (`probe_git`, `probe_ci`, `probe_deploy`)
   - **Requirements**:
     - Collect Git merge-base and unpushed/unmerged commits against `origin/main`.
     - Collect latest GitHub Actions run status for current SHA (`gh run list/view`).
     - Collect active Cloudflare Workers deployment ID and bundle hash (`wrangler deployments`).

5. **Storage Retention & Private State Containment**
   - **Target Seam**: `host/agent_hub_host/session_cleanup.py`
   - **Requirements**:
     - Enforce soft limit (128 MiB) and hard limit (256 MiB) on `.local/` state directories.
     - Automatically rotate and gzip compress operational logs when exceeding 4 MiB.

---

### Priority 2: Extended Analytics & Diagnostics

These signals provide historical analysis and telemetry without blocking production operations.

1. **Token Telemetry & Cost Accounting**
   - **Target Seam**: `host/agent_hub_host/metrics_producer.py`, `src/metrics.ts`
   - **Requirements**:
     - Ingest cumulative token counts from native harness transcripts and OpenCode DB (`opencode.db` mode=ro).
     - Track observation interval deltas separately from lifetime cumulative totals.
     - Treat provider-reported costs as non-authoritative estimates, not subscription bills.

2. **Task Evidence Verification**
   - **Target Seam**: Hub task intake service (`src/intake.ts`)
   - **Requirements**:
     - Verify existence and mtime of registered file paths in task `evidence_paths`.
     - Reject recursive directory scans; report missing or out-of-scope paths.

3. **Approval Modal Stall Detection (`aplexer-watch`)**
   - **Target Seam**: `host/agent_hub_host/session_host.py`
   - **Requirements**:
     - Periodically capture terminal screens for interactive sessions.
     - Detect repeated modal prompts (e.g. approval dialogs) and notify or auto-enter if configured.

---

## 4. Architectural Mapping to Agent Hub Seams

```
+-----------------------------------------------------------------------------------+
|                                  AGENT HUB                                        |
|                                                                                   |
|  [ Cloudflare Workers Durable Object (Hub DO) ]                                  |
|  +-----------------------------------------------------------------------------+  |
|  | - src/checks.ts: Central evaluator for liveness, quota, inbox_backlog,     |  |
|  |   http, and custom system health checks. Reconciles alerts.                 |  |
|  | - src/status.ts: Status projection joining tasks, sessions, host heartbeats,|  |
|  |   and observations (session, report, probe_git, probe_ci, probe_deploy).    |  |
|  | - src/monitoring.ts (Peer stream): Ingests filesystem disk usage & alerts.   |  |
|  | - src/metrics.ts: Aggregates host metrics payloads & TSDB summaries.       |  |
|  | - src/session_lifecycle.ts: Classifies stalls, resting contradictions, SLO. |  |
|  +-----------------------------------------------------------------------------+  |
|                                         ^                                         |
|                                         | HTTPS (Bearer Machine Auth)             |
|                                         v                                         |
|  [ Host Runtime (Hetzner / host/agent_hub_host/) ]                               |
|  +-----------------------------------------------------------------------------+  |
|  | - session_host.py: Single-inflight bounded observer loop executing:         |  |
|  |     * disk_monitor.py: statvfs scan on /, /home, /data, /tmp + top consumers|  |
|  |     * status_collect.py: Native sessions + /proc/{pid}/stat liveness        |  |
|  |     * status_probe.py: git ancestry, CI run status, wrangler deploy sha     |  |
|  |     * metrics_producer.py: host load, memory, token counters -> /api/metrics |  |
|  |     * quota.py: quse snapshot evaluator (15% reserve)                       |  |
|  +-----------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------+
```

---

## 5. Verification Runbook & Commands

To verify monitoring signals locally without touching external services:

1. **Verify Local Checks Evaluator (DO vitest)**:
   ```bash
   npx vitest run test/checks.test.ts
   ```

2. **Verify Host Status Probes**:
   ```bash
   python3 -m unittest host/tests/test_status_probe.py
   ```

3. **Verify Host Disk Monitor (Peer Stream)**:
   ```bash
   python3 -m unittest host/tests/test_disk_monitor.py
   ```

4. **Verify Metrics Ingestion & Validation**:
   ```bash
   npx vitest run test/metrics.test.ts
   ```

5. **Dry-Run Cloudflare Worker Deployment**:
   ```bash
   npx wrangler deploy --dry-run --outdir dist
   ```

---

## 6. Comprehensive Signal Cross-Mapping (Inventory to Agent Hub)

Every signal from [docs/MONITORING-INVENTORY.md](MONITORING-INVENTORY.md) is cross-mapped below to its concrete coverage status in Agent Hub:

| Inventory Signal | Source in Competition | Current Hub Status | Implementation Seam in Hub | Notes & Limitations |
| :--- | :--- | :--- | :--- | :--- |
| **Agent Bus Reachability** | `scripts/coordination/agent_bus_transport.py` | **Current Coverage** | `src/hub.ts`, `src/bus.ts` (`/api/messages`) | Native DO-backed message bus replaces ad-hoc HTTP adapter :8788. |
| **Inbox Backlog & Staleness** | `scripts/ping/inbox-guard.py` | Source; activation pending | `src/checks.ts` (`kind: 'inbox_backlog'`) | The inbox_backlog check kind exists, but GET/api/checks currently has only enabled liveness. Actor-specific backlog checks are not configured. |
| **Claims Expiry & Lease Audit** | `scripts/ping/claims-audit.py` | **Current Coverage** | Hub `leases` table + `src/leases.ts` | Linearizable SQLite lease records with epoch fencing replace FileBus claims. |
| **Desktop Mailbox Watchdog** | `desktop-orchestrator-watchdog.sh` | Partial; legacy retained | `host/agent_hub_host/session_host.py` | `session_host.py` supervises its own Hub controller, not the desktop orchestrator mailbox. No replacement watchdog or retirement proof. |
| **Process Liveness & Anti-Reuse** | `scripts/metrics/collect.py` (`proc()`) | **Current Coverage** | `host/agent_hub_host/native_sessions.py` | Uses `/proc/{pid}/stat` `start_ticks` to prevent PID reuse. |
| **First-Action Execution Receipt** | `scripts/delivery/validate_backlog.py` | **Current Coverage** | Launcher scripts + `.local/first-action-*.json` | Strict verification before admitting worker session. |
| **Unexplained Idle Over SLO** | `scripts/supervision/service.py` | **Current Coverage** | `src/session_lifecycle.ts` | Stalled/stuck classification in Hub DO lifecycle state engine. |
| **Resting State Contradiction** | `scripts/supervision/service.py` | **Implemented 6b Port** | `src/session_lifecycle.ts` | Flags sessions reporting idle/waiting with recent activity (`contradicted_resting_state: true`), retaining active classification. |
| **Pending Message Delivery SLO** | `scripts/supervision/service.py` | Source; activation pending | `src/checks.ts` (`kind: 'delivery_slo'`) | The delivery_slo check kind is implemented; no enabled scheduled delivery_slo check was present at audit. Real idle-model guarded delivery acceptance is also pending. |
| **Consecutive Ping Misses** | `scripts/ping/principal-ping.py` | Partial; legacy retained | Hub Health Model (SPEC §9) | Machine heartbeats exist; principal-wake execution/recovery remains M8b work. No replacement proof for the legacy consecutive-miss monitor. |
| **Approval Modal Stalls** | `python3 -m aplexer_watch` | **Remaining Item** | Standalone Hetzner service during migration | Running via systemd `aplexer-watch.service` until host native capture integration. |
| **Turn-End Wake Timer Audit** | `scripts/ping/wake-audit.py` | Partial; legacy retained | Serverless DO Triggers / Cron | Worker cron exists, but no current turn-end wake-obligation audit or installed principal-wake backend is proved. |
| **Provider Window Quota** | `scripts/quota-monitor.py` | **Current Coverage** | `src/quota.ts`, `host/agent_hub_host/quota.py` | `/api/quota` enforces fail-closed 15% reserve gate across providers. |
| **Launcher Admission Routes** | `scripts/supervision/service.py` | **Current Coverage** | `src/model-routing.ts`, launcher gates | Enforces model quota reserve before launching workers. |
| **Monthly Cloud Budget** | `scripts/delivery/cloud_budget.py` | Policy only; collection missing | Cloudflare Workers Free Tier policy | The USD5/month boundary is a policy. Free-tier architecture is not measured account usage, monthly cost, or preventive-budget monitoring. |
| **Filesystem Disk Usage** | `scripts/metrics/collect.py` | **Implemented Addendum 6** | `host/agent_hub_host/disk_monitor.py`, `src/monitoring.ts` | Multi-mount statvfs (`/`, `/home`, `/data`, `/tmp`) with 85% warn / 95% critical thresholds. |
| **Top Space Consumers** | *Missing in competition* | **Implemented Addendum 6** | `host/agent_hub_host/disk_monitor.py` (`scan_top_consumers`) | Bounded traversal (depth, entries, time) triggered only on threshold violations. |
| **Host Load Average** | `scripts/metrics/collect.py` | **Implemented 6b Port** | `host/agent_hub_host/host_health_monitor.py`, `src/monitoring.ts`, `src/status.ts` | Collects Linux `os.getloadavg()`, ingests to Hub DO, exposes via `/api/monitoring/health` and `/api/status`. |
| **Host Memory (MemAvailable/total)** | `scripts/metrics/collect.py` | **Implemented 6b Port** | `host/agent_hub_host/host_health_monitor.py`, `src/monitoring.ts`, `src/status.ts` | Parses `/proc/meminfo`, evaluates memory pressure (>=85% warn, >=95% critical), surfaces in `needs_attention`. |
| **Worker Process RSS & CPU** | `scripts/metrics/collect.py` | **Current Coverage** | `host/agent_hub_host/metrics_producer.py`, `src/metrics.ts` | Aggregates per-session utime, stime, RSS to Hub `/api/metrics`. |
| **Cgroup Memory & Tasks Containment** | `scripts/supervision/service.py` | **Current Coverage** | Systemd user units / launcher limits | MemoryMax=1500M, TasksMax=256 enforced per agent worker unit. |
| **Local Private Storage Guard** | `scripts/supervision/retention.py` | Missing automatic size guard | `host/agent_hub_host/session_cleanup.py` | `session_cleanup.py` confines cleanup paths and preserves evidence; it does not implement the claimed128/256MiB storage-size guard. Existing retention monitoring remains needed. |
| **Git Ancestry & Remote Drift** | `scripts/checks/status_probe.py` | Partial; configured probes active | `host/agent_hub_host/status_probe.py` (`probe_git`) | Exact configured SHA/ref ancestry probes run for allowlisted Hub sessions. They do not collect every repository or prove remote-ref freshness/divergence generally. |
| **CI Workflow Status Drift** | `scripts/checks/status_probe.py` | **Current Coverage** | `host/agent_hub_host/status_probe.py` (`probe_ci`) | Surfaces latest GitHub Actions status for current HEAD SHA. |
| **Deployed Artifact Bundle Drift** | `scripts/checks/status_probe.py` | Partial; active deployment proved | `host/agent_hub_host/status_probe.py` (`probe_deploy`) | Authenticated Cloudflare deployment/version annotations match exact source SHA. No artifact bundle hash is collected; control-plane deployment and actual DO request rollover are separately verified. |
| **Task Registry Concurrency (CAS)** | `scripts/delivery/tasks_guard.py` | **Implemented Bugfix** | `src/monitoring.ts` -> `src/intake.ts` | Enforces atomic task version CAS, user history preservation, and bounded episode deduplication. |
| **Repo Hygiene & Secret Scans** | `scripts/checks/secrets.sh` | Partial; no collector proved | `scripts/ci/`, gitignore policy | CI validates workflow token references and repository policy forbids secrets; this does not prove a comprehensive secret-scan or hygiene monitor is running. |
| **Metrics Collector Staleness** | `scripts/root/headless-root-run.sh` | **Implemented 6b Port** | `src/monitoring.ts`, `src/status.ts` | Evaluates observation age; marks `fresh` (<180s), `stale` (180s-900s), or `missing` (>900s). |
| **Supervisor Status Staleness** | `supervision_watcher.sh` | Partial; legacy retained | Hub DO Health Engine | Hub controller heartbeat does not replace the competition supervisor status-file watchdog without target-specific observation and recovery proof. |
| **Host Agent Check-in Stale/Dead** | `docs/SPEC.md` §9 | **Current Coverage** | `src/status.ts` (`isStaleHost`) | Host heartbeat staleness evaluated dynamically in `/api/status`. |
| **Headless Root Heartbeat Stale** | `scripts/root/headless-root-run.sh` | Partial; legacy retained | Hub root job scheduler and lease fencing | Role/root lease source exists. Actual legacy headless-root heartbeat ingestion and equivalent installed recovery are not proved; keep the old timer/monitor. |
| **Task Failure & Error Trapping** | `scripts/delivery/validate_backlog.py` | **Current Coverage** | `src/intake.ts`, `src/status.ts` | Captures error strings, timeout codes, failure counters, and next actions. |

