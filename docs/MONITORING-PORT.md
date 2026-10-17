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
