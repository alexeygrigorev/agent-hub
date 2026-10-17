# Monitoring Inventory: Cloudflare Competition & Installed System Infrastructure

- **Canonical Parent Hub Task**: `75e79822-6996-484f-8aee-194d0be39d10` (Alexey Addendum 6b)
- **Author**: `platform-monitor-inventory`
- **Source Repository**: `/home/alexey/git/cloudflare-agent-git`
- **Source Revision**: `642110a99ad4e635d1b48fdfda4f9a5ff75d1e39` (`642110a`)
- **Target Seam in Agent Hub**: `docs/MONITORING-INVENTORY.md`, cross-referenced with `docs/MONITORING-PORT.md`

---

## 1. Overview and Intent

Following Alexey's Addendum 6b directive, this document provides an exhaustive, authoritative inventory of all monitoring, health checks, alerts, watchdogs, dashboards, and background cron/systemd checks developed during the Cloudflare competition (`cloudflare-agent-git`) and installed on the primary Hetzner host (`DEFLf4-rahKf9wE4TQAZfA`).

The objective is to establish an exact baseline of what was monitored, how signals were evaluated, what thresholds and cadences were enforced, and how each monitoring capability maps to Agent Hub's serverless Cloudflare Workers Durable Object architecture and host runtime seams.

---

## 2. Source Environment & Installed Infrastructure (Hetzner)

### 2.1 Installed Systemd User Timers & Services

The Hetzner host actively maintains the following user-level timers and services (sanitized, omitting private tokens and addresses):

| Timer Unit | Service Unit | Schedule / Cadence | Target Executable / Script | Description |
| :--- | :--- | :--- | :--- | :--- |
| `supervision-watcher-1.timer` | `supervision-watcher@1.service` | Every 60s (`OnUnitActiveSec=1min`) | `scripts/supervision/systemd/supervision_watcher.sh` | Restarts supervisor if dead or `status.json` > 600s stale; runs metrics retention. |
| `supervision-watcher-2.timer` | `supervision-watcher@2.service` | Every 60s (`OnUnitActiveSec=1min`) | `scripts/supervision/systemd/supervision_watcher.sh` | Redundant / offset watcher ensuring supervision loop survival. |
| `desktop-orchestrator-watchdog.timer` | `desktop-orchestrator-watchdog.service` | Every 60s (`OnUnitActiveSec=60s`) | `~/.local/bin/desktop-orchestrator-watchdog.sh` | Ensures root's desktop mailbox aplexer session remains alive. |
| `agent-hub-fleet-host.timer` | `agent-hub-fleet-host.service` | Every 60s (`OnUnitInactiveSec=60s`) | `agent_hub_host.cli fleet-host --cycles 1` | Bounded host heartbeat, subscription, and session report cycle to Hub. |
| `principal-ping.timer` | `principal-ping.service` | Every 10 min (`OnUnitInactiveSec=10min`) | `scripts/ping/principal-ping.py --live` | Liveness ping to principal session; triggers recovery after 3 consecutive misses. |
| `aplexer-watch.timer` | `aplexer-watch.service` | Every 30 min (`OnUnitActiveSec=30min`) | `python3 -m aplexer_watch` | Screen capture diff and automated Enter response for approval modal stalls. |
| `headless-root.timer` | `headless-root.service` | 30 min interval / on inactive | `scripts/root/headless-root-launch.sh` | Codex headless root run (single-instance lock, 25m hard limit, metrics guard). |
| *N/A (long-running)* | `experiment-metrics.service` | Continuous daemon (`Restart=always`) | `scripts/metrics/collect.py --loop --serve --interval 60 --port 8766` | Host metrics collector and HTTP server on `127.0.0.1:8766`. |
| *N/A (long-running)* | `victoriametrics.service` | Continuous daemon (`Restart=always`) | `victoria-metrics -httpListenAddr=127.0.0.1:8428 -retentionPeriod=400d` | Single-node TSDB storing experiment metrics, capped at 1500M RAM. |
| *N/A (long-running)* | `supervision.service` | Oneshot / managed session | `scripts/supervision/systemd/launch_supervision.sh` | Starts `scripts/supervision/service.py` inside aplexer session `experiment-supervision`. |
| *N/A (long-running)* | `agent-hub-session-host.service` | Continuous daemon (`Restart=on-failure`) | `session-host-supervisor.py` | Hub native session host supervisor managing local worker sessions. |
| *N/A (long-running)* | `agent-bus-win35-adapter.service` | Continuous daemon (`Restart=on-failure`) | `agent-bus/.../adapter.py --port 8788` | Secure endpoint adapter for outbound Windows / bus connections. |

### 2.2 Installed User Crontab Jobs

- `0 3 * * * /home/alexey/git/aplexer/tools/esc-zcodex/esc_zcodex >> ~/.local/state/esc-zcodex.log 2>&1`: Nightly Esc sweep of live zcodex aplexer sessions at 03:00 Europe/Berlin.
- Daily scheduled pipeline runs for RDS database syncs (cmp, aisl, website, relay) with `flock` guards.

---

## 3. Comprehensive Monitored Signals Inventory

### 3.1 Service, Network & Message Bus Monitoring

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Agent Bus Reachability** | `scripts/coordination/agent_bus_transport.py` | Per message / 5s timeout | HTTP response 200/204; failover to local FileBus on error | Live | Hub `/api/messages` Durable Object bus; replaces adapter :8788. |
| **Inbox Backlog & Staleness** | `scripts/ping/inbox-guard.py` | Turn end & hook invocations | Unread messages prompted max 3 times (`CAP=3`), 10m cooldown (`COOLDOWN=600s`) | Live | `src/checks.ts` (`inbox_backlog` check kind with `max_age_s`). |
| **Claims Expiry & Lease Audit** | `scripts/ping/claims-audit.py`, `scripts/checks/claims.sh` | 30 min audit, pre-commit hook | Claim TTL 30m (`CLAIM_TTL_MINUTES=30`), reminder every 30m | Live | Hub `leases` table with epoch fencing tokens; replaces FileBus claims. |
| **Desktop Mailbox Watchdog** | `~/.local/bin/desktop-orchestrator-watchdog.sh` | 60s timer | Checks `desktop-orchestrator` session alive; respawns if dead | Live | Replaced by Hub native session host supervisor (`session_host.py`). |

### 3.2 Process & Worker Health, First-Action, Stalls & Obligations

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Process Liveness & Anti-Reuse** | `scripts/metrics/collect.py` (`proc()`) | 60s | Parses `/proc/{pid}/stat`; verifies start ticks (`start_ticks`) to prevent PID reuse; flags zombie (`Z`) | Live | `host/agent_hub_host/native_sessions.py` tracks native PID + cgroup. |
| **First-Action Execution Receipt** | `scripts/delivery/validate_backlog.py`, launcher admission | On worker start | Task in `running` state requires genuine `aplexer whoami --json` receipt file before proceeding | Live | Handled by maintained launcher / `launch-hub-*.json` first-action validation. |
| **Unexplained Idle Over SLO** | `scripts/supervision/service.py` (`idle_episode()`) | 60s | Active ready tasks exist + entity continuously idle >= 180s (3m) without pending message | Live | `src/session_lifecycle.ts` (stalled/stuck classification in Hub DO). |
| **Resting State Contradiction** | `scripts/supervision/service.py` (`check_resting_state_contradicted()`) | 60s | `last_activity_ms > reported_state_at_ms + 1000` (PTY activity after reported idle state) | Live | `src/session_lifecycle.ts` (contradicted resting state logic). |
| **Pending Message Delivery SLO** | `scripts/supervision/service.py` (`check_pending_slo()`) | 60s | Pending message age >= 300s (5m) for standard agents, 1800s (30m) for Claude principal | Live | Hub message dispatch tracking & retry limits. |
| **Consecutive Ping Misses** | `scripts/ping/principal-ping.py` | 10 min timer | Ping timeout 300s; 3 consecutive misses (`PING_MISS_THRESHOLD=3`) triggers recovery; stuck threshold 900s | Live | Hub health model (§9) replaces with principal wake jobs and host heartbeats. |
| **Approval Modal Stalls** | `aplexer-watch` (`aplexer_watch`) | 30 min timer | Compares screen terminal hashes; auto-Enters sessions stuck on confirmation modals | Live | `host/agent_hub_host/session_host.py` / `aplexer-watch.service`. |
| **Turn-End Wake Timer Audit** | `scripts/ping/wake-audit.py`, `turn-end-check.py` | 15 min check, turn hooks | Every waiting session must have an armed wake <= 30 min (`MAX_INTERVAL_S=1800`); 3 strikes escalate | Live | Hub scheduled triggers / timer jobs. |

### 3.3 Quota & Budget Monitoring

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Provider Window Quota** | `scripts/quota-monitor.py`, `scripts/quota-gate.py` | 60s | Calls `quse codex --json` (45s timeout); requires `limit_reached=False` and min window > 15% reserve | Live | Hub `/api/quota` snapshot endpoint (`src/quota.ts`, `host/agent_hub_host/quota.py`). |
| **Launcher Admission Routes** | `scripts/supervision/service.py`, launcher ranking | Per task submission | Requires explicit `model_requirements`; Gemini / GLM admitted if remaining quota > 18% / 42% | Live | `src/model-routing.ts` candidate selection and quota evaluation. |
| **Monthly Cloud Budget** | `scripts/delivery/cloud_budget.py` | Batch audit / on change | Month-to-date aggregate cloud spend <= $5.00 USD; fails closed if service costs unknown | Live | Built-in Free plan constraint; Hub operates within Cloudflare Free tier. |

### 3.4 Containment, RAM, Host Load & Disk Space

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Filesystem Disk Usage** | `scripts/metrics/collect.py`, `scripts/principal_metrics.py` | 60s | `statvfs` on `/` and `/tmp`; target free disk >= 20 GiB (`TARGETS["free_disk_gib"]=20`) | Live | **CRITICAL GAP in competition**: missed `/data` (which hit 100%). Now owned by Addendum 6 disk monitor. |
| **Top Space Consumers** | *Missing in competition* | Triggered on threshold | Scans top disk space consumers when usage >= 85% warn or >= 95% critical | Live (New) | Owned by `platform-disk-monitor` (`host/agent_hub_host/disk_monitor.py`). |
| **Host Load Average** | `scripts/metrics/collect.py` | 60s | Reads `os.getloadavg()` (1m, 5m, 15m load) | Live | Projected in `/api/metrics` payload. |
| **Worker Process RSS & CPU** | `scripts/metrics/collect.py` (`proc()`) | 60s | Reads CPU utime/stime and RSS pages from `/proc/{pid}/stat` | Live | Hub metrics producer and host session monitoring. |
| **Cgroup Memory & Tasks Containment** | `scripts/supervision/service.py`, systemd units | Continuous systemd enforcement | `MemoryMax<=1500M`, `TasksMax<=100` (or 256) per transient unit | Live | Launcher transient units and aplexer session containment cgroups. |
| **Local Private Storage Guard** | `scripts/supervision/retention.py` | Per write / event cycle | Soft limit 128 MiB (`SOFT_BYTES`), hard limit 256 MiB (`HARD_BYTES`); gzip archives events > 4 MiB | Live | Hub `.local/` state rotation; host journal limits. |

### 3.5 Source, Deployed & CI Drift

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Git Ancestry & Remote Drift** | `scripts/checks/status_probe.py`, `worktrees.sh` | Per probe / status round | `git rev-parse`, `merge-base` vs `origin/main`; detects unintegrated or divergent commits | Live | `host/agent_hub_host/status_probe.py` (`probe_git` observation kind). |
| **CI Workflow Status Drift** | `scripts/checks/status_probe.py` | Per probe round | `gh run list/view` for active commit SHA; checks pass/fail | Live | `host/agent_hub_host/status_probe.py` (`probe_ci` observation kind). |
| **Deployed Artifact Bundle Drift** | `scripts/checks/status_probe.py` | Per probe round | `wrangler deployments` active deployment hash vs local HEAD SHA | Live | `host/agent_hub_host/status_probe.py` (`probe_deploy` observation kind). |
| **Task Registry Concurrency (CAS)** | `scripts/delivery/tasks_guard.py` | On registry write | Hash verification & generation lock; rejects dropped IDs or un-audited status downgrades | Live | Hub DO SQLite transactions provide linearizable optimistic concurrency natively. |
| **Repo Hygiene & Secret Scans** | `scripts/checks/secrets.sh`, `hygiene.sh` | Pre-commit / pre-push | Scans diffs for API keys, tokens, private paths, internal machine IPs | Live | Local development checks; CI automation. |

### 3.6 Metric Freshness & Execution Failure Detection

| Signal / Dimension | Source Location (`cloudflare-agent-git`) | Cadence / Interval | Threshold / Condition | Live vs Research | Hub Existing Seam & Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Metrics Collector Staleness** | `scripts/root/headless-root-run.sh` | Every 30 min | `latest.json` mtime older than 600s (10m) triggers restart of `experiment-metrics.service` | Live | `src/metrics.ts` (`METRICS_STALE_MS = 7_200_000` ms = 2h; host collector 300s). |
| **Supervisor Status Staleness** | `scripts/supervision/systemd/supervision_watcher.sh` | Every 60s | `.local/supervision/status.json` age > 600s (10m) triggers supervisor restart | Live | Hub health model replaces with DO heartbeat evaluation. |
| **Host Agent Check-in Stale/Dead** | `docs/SPEC.md` §9 | 60s heartbeat | Heartbeat age 3–15 min = `stale`; > 15 min (900s) = `unavailable` | Live | Hub DO `machines` table + `isStaleHost` in `src/status.ts`. |
| **Headless Root Heartbeat Stale** | `scripts/root/headless-root-run.sh` | Continuous during run | Heartbeat age > 180s (3m) = stale; successor terminates process group (`kill -TERM -<pgid>`) | Live | Hub root job scheduler and lease fencing. |
| **Task Failure & Error Trapping** | `scripts/delivery/validate_backlog.py` | Per task audit | Captures exit codes, timeout status (`124/137`), error strings in summary JSON | Live | Hub intake task lifecycle (`error`, `failure_count`, `next_action`). |

---

## 4. Key Gaps in Competition Monitoring vs Agent Hub Architecture

1. **Blindness to Secondary Mounts (`/data`, `/home`)**:
   - The competition's metrics collector (`scripts/metrics/collect.py`) monitored only `/` and `/tmp`.
   - On 2026-10-17, `/data` on Hetzner reached 100% capacity, breaking operations silently because it was not in the hardcoded list `[('root', '/'), ('tmp', '/tmp')]`.
   - Addendum 6 addresses this directly by introducing dynamic filesystem enumeration via `statvfs` for all meaningful mounts (`/`, `/home`, `/data`, `/tmp`, bind mounts).
2. **Proliferation of Standalone Local Watcher Daemons**:
   - The competition relied on numerous uncoordinated shell loops and systemd timers (`supervision_watcher.sh`, `desktop-orchestrator-watchdog.sh`, `principal-ping.sh`, `aplexer-watch`).
   - Agent Hub centralizes evaluation in the Durable Object (`src/checks.ts`, `src/status.ts`) and delegates collection to a single bounded host cycle (`session_host.py` / `agent_hub_host.cli fleet-host`).
3. **Absence of Top-Consumer Disk Diagnostics**:
   - When disk thresholds were crossed in the competition, human intervention was required to run ad-hoc `du` commands.
   - Hub disk monitoring adds automated, bounded top-consumer directory scans triggered only when warn (>=85%) or critical (>=95%) thresholds are exceeded.
