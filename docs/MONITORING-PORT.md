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

## 2. Explicit Duplicates and Superseded Checks

The following components and checks from `cloudflare-agent-git` are **explicitly superseded** and must **NOT** be ported into Agent Hub:

| Legacy Component / Check | Source File | Reason for Superseding / Non-Port | Replacement in Agent Hub |
| :--- | :--- | :--- | :--- |
| **Cross-Model Review Gate** | `scripts/guards/review_gate.py` | **Explicitly cancelled by Alexey (2026-10-17)**: review gates and paperwork freezes removed to prioritize shipping tested code directly to `main`. | Small, focused commits straight to `main` with `[skip ci]`. |
| **Autonomous Supervision Daemon** | `scripts/supervision/service.py`, `launch_supervision.sh` | Running a heavy standalone Python supervisor daemon in an aplexer session is brittle and caused cascading unresponsiveness. | Hub DO `Checks` (`src/checks.ts`) + host `session_host.py` bounded single-inflight observer threads. |
| **Supervisor Watchdog Timers** | `supervision-watcher-1.timer`, `supervision_watcher.sh` | Brittle dual-systemd-timer arrangement polling `status.json` age. | Hub host heartbeat loop (`agent-hub-fleet-host.timer` or `session_host.py`). |
| **Desktop Mailbox Watchdog** | `desktop-orchestrator-watchdog.sh`, `.timer` | Ad-hoc shell script watching a dedicated desktop mailbox aplexer session. | Hub message bus (`/api/messages`) and native session host (`session_host.py`). |
| **Standalone Principal Ping** | `scripts/ping/principal-ping.py`, `.timer` | 10-minute ping loop attempting ad-hoc recovery outside central coordination. | Hub health model (§9) + scheduled principal-wake jobs. |
| **FileBus Pre-Commit Claims** | `scripts/checks/claims.sh`, `scripts/guards/claims_check.py` | Local filesystem bus lock files blocking git commits. | Hub `leases` table with epoch fencing tokens; claims are runtime leases, never git commit blockers. |
| **Machine Status Git Linter** | `scripts/checks/machine-status.sh` | Grepped markdown documentation to prohibit mentioning machine availability. | Replaced by live Hub `machines` registry and runtime heartbeat status. |
| **Strict Commit Message Linter** | `scripts/checks/commit-message.sh` | Required rigid `type(scope): subject` syntax, rejecting standard git workflows. | Standard concise commit messages without gate failure. |

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
| **Inbox Backlog & Staleness** | `scripts/ping/inbox-guard.py` | **Current Coverage** | `src/checks.ts` (`kind: 'inbox_backlog'`) | Evaluates pending unread messages past configured `max_age_s`. |
| **Claims Expiry & Lease Audit** | `scripts/ping/claims-audit.py` | **Current Coverage** | Hub `leases` table + `src/leases.ts` | Linearizable SQLite lease records with epoch fencing replace FileBus claims. |
| **Desktop Mailbox Watchdog** | `desktop-orchestrator-watchdog.sh` | **Superseded** | `host/agent_hub_host/session_host.py` | Standalone session supervisor replaced by native session host controller. |
| **Process Liveness & Anti-Reuse** | `scripts/metrics/collect.py` (`proc()`) | **Current Coverage** | `host/agent_hub_host/native_sessions.py` | Uses `/proc/{pid}/stat` `start_ticks` to prevent PID reuse. |
| **First-Action Execution Receipt** | `scripts/delivery/validate_backlog.py` | **Current Coverage** | Launcher scripts + `.local/first-action-*.json` | Strict verification before admitting worker session. |
| **Unexplained Idle Over SLO** | `scripts/supervision/service.py` | **Current Coverage** | `src/session_lifecycle.ts` | Stalled/stuck classification in Hub DO lifecycle state engine. |
| **Resting State Contradiction** | `scripts/supervision/service.py` | **Implemented 6b Port** | `src/session_lifecycle.ts` | Flags sessions reporting idle/waiting with recent activity (`contradicted_resting_state: true`), retaining active classification. |
| **Pending Message Delivery SLO** | `scripts/supervision/service.py` | **Implemented 6b Port** | `src/checks.ts` (`kind: 'delivery_slo'`) | Detects unacknowledged active messages past SLA threshold (`max_age_s`). |
| **Consecutive Ping Misses** | `scripts/ping/principal-ping.py` | **Superseded** | Hub Health Model (SPEC §9) | Replaced by Hub DO machine heartbeats and principal wake jobs. |
| **Approval Modal Stalls** | `python3 -m aplexer_watch` | **Remaining Item** | Standalone Hetzner service during migration | Running via systemd `aplexer-watch.service` until host native capture integration. |
| **Turn-End Wake Timer Audit** | `scripts/ping/wake-audit.py` | **Superseded** | Serverless DO Triggers / Cron | Hub replaces brittle turn-end wake audit with managed background alarms. |
| **Provider Window Quota** | `scripts/quota-monitor.py` | **Current Coverage** | `src/quota.ts`, `host/agent_hub_host/quota.py` | `/api/quota` enforces fail-closed 15% reserve gate across providers. |
| **Launcher Admission Routes** | `scripts/supervision/service.py` | **Current Coverage** | `src/model-routing.ts`, launcher gates | Enforces model quota reserve before launching workers. |
| **Monthly Cloud Budget** | `scripts/delivery/cloud_budget.py` | **Current Coverage** | Cloudflare Workers Free Tier policy | Budget capped at $5/mo total; zero-cloud-spend architecture. |
| **Filesystem Disk Usage** | `scripts/metrics/collect.py` | **Implemented Addendum 6** | `host/agent_hub_host/disk_monitor.py`, `src/monitoring.ts` | Multi-mount statvfs (`/`, `/home`, `/data`, `/tmp`) with 85% warn / 95% critical thresholds. |
| **Top Space Consumers** | *Missing in competition* | **Implemented Addendum 6** | `host/agent_hub_host/disk_monitor.py` (`scan_top_consumers`) | Bounded traversal (depth, entries, time) triggered only on threshold violations. |
| **Host Load Average** | `scripts/metrics/collect.py` | **Implemented 6b Port** | `host/agent_hub_host/host_health_monitor.py`, `src/monitoring.ts`, `src/status.ts` | Collects Linux `os.getloadavg()`, ingests to Hub DO, exposes via `/api/monitoring/health` and `/api/status`. |
| **Host Memory (MemAvailable/total)** | `scripts/metrics/collect.py` | **Implemented 6b Port** | `host/agent_hub_host/host_health_monitor.py`, `src/monitoring.ts`, `src/status.ts` | Parses `/proc/meminfo`, evaluates memory pressure (>=85% warn, >=95% critical), surfaces in `needs_attention`. |
| **Worker Process RSS & CPU** | `scripts/metrics/collect.py` | **Current Coverage** | `host/agent_hub_host/metrics_producer.py`, `src/metrics.ts` | Aggregates per-session utime, stime, RSS to Hub `/api/metrics`. |
| **Cgroup Memory & Tasks Containment** | `scripts/supervision/service.py` | **Current Coverage** | Systemd user units / launcher limits | MemoryMax=1500M, TasksMax=256 enforced per agent worker unit. |
| **Local Private Storage Guard** | `scripts/supervision/retention.py` | **Current Coverage** | `host/agent_hub_host/session_cleanup.py` | Soft 128 MiB, hard 256 MiB limits on `.local/` state directories. |
| **Git Ancestry & Remote Drift** | `scripts/checks/status_probe.py` | **Current Coverage** | `host/agent_hub_host/status_probe.py` (`probe_git`) | Computes divergence, unmerged commits against `origin/main`. |
| **CI Workflow Status Drift** | `scripts/checks/status_probe.py` | **Current Coverage** | `host/agent_hub_host/status_probe.py` (`probe_ci`) | Surfaces latest GitHub Actions status for current HEAD SHA. |
| **Deployed Artifact Bundle Drift** | `scripts/checks/status_probe.py` | **Current Coverage** | `host/agent_hub_host/status_probe.py` (`probe_deploy`) | Queries active Cloudflare Workers deployment ID and bundle hash. |
| **Task Registry Concurrency (CAS)** | `scripts/delivery/tasks_guard.py` | **Implemented Bugfix** | `src/monitoring.ts` -> `src/intake.ts` | Enforces atomic task version CAS, user history preservation, and bounded episode deduplication. |
| **Repo Hygiene & Secret Scans** | `scripts/checks/secrets.sh` | **Current Coverage** | `scripts/ci/`, gitignore policy | Strict ban on committing secrets, private paths, or API tokens. |
| **Metrics Collector Staleness** | `scripts/root/headless-root-run.sh` | **Implemented 6b Port** | `src/monitoring.ts`, `src/status.ts` | Evaluates observation age; marks `fresh` (<180s), `stale` (180s-900s), or `missing` (>900s). |
| **Supervisor Status Staleness** | `supervision_watcher.sh` | **Superseded** | Hub DO Health Engine | Centralized DO heartbeat evaluations supersede brittle local watcher files. |
| **Host Agent Check-in Stale/Dead** | `docs/SPEC.md` §9 | **Current Coverage** | `src/status.ts` (`isStaleHost`) | Host heartbeat staleness evaluated dynamically in `/api/status`. |
| **Headless Root Heartbeat Stale** | `scripts/root/headless-root-run.sh` | **Current Coverage** | Hub root job scheduler and lease fencing | Epoch fencing and heartbeat tracking in `leases` table. |
| **Task Failure & Error Trapping** | `scripts/delivery/validate_backlog.py` | **Current Coverage** | `src/intake.ts`, `src/status.ts` | Captures error strings, timeout codes, failure counters, and next actions. |

