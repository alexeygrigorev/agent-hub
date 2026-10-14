# Agent Hub — specification

Status: draft v0.1, 11 October 2026. Author: root (Grok Bot 'Cloudflare Root') for Alexey. Owner of implementation: `agent-hub-head`.

## 1. Why

Root currently polls every 30 minutes from the Grok Bot (and earlier from the Win35 route). That burns model usage just to look at state. The cross-machine plumbing is also spread over several pieces that each solve part of the problem: a JSON machine list, an mTLS bus adapter on Hetzner, a (stood-down) role authority, a file mailbox for the desktop, a supervision shell session, a principal ping timer and a separate dashboard.

Agent Hub replaces those with one small serverless service plus one small outbound agent per host:

1. Every 30 minutes the hub launches a **headless root run** on a random available host via the Agent Quota Launcher. That run checks the principal and does the root duties (`cloudflare-agent-git/_docs/team/02-root.md`, `recovery/README.md`, `recovery/principal.md`, `recovery/head.md`), then exits.
2. It launches the **daily writer** job directly (`_docs/team/07-writer.md`, `website/write_daily.py`).
3. Machines **register** themselves (replaces `coordination/MACHINES.json` + `scripts/machines.py` availability).
4. It is the **message bus** between projects, agents and machines (replaces the Agent Bus adapter on Hetzner :8788, the role authority on :8801 and the desktop-orchestrator file mailbox). Home machines are outbound-only; nothing connects into them and no cross-machine path needs SSH.
5. It knows the **health** of the principal, every head, every host, root runs and the writer (replaces experiment-supervision status and principal-ping).
6. It serves a **dashboard** (replaces/absorbs agent-dashboard's views).

Non-goals: running model processes in the cloud; replacing the Agent Quota Launcher, aplexer, Agent Branches or quota gates; storing private evidence, transcripts, credentials or raw account/billing/credit quota balances in the hub (except the founder-authorized `QUOTA-HUB-SPEC-20261014` sanitized per-host remaining-percentage snapshot on `/api/quota`, see [docs/QUOTA.md](QUOTA.md)); paid Cloudflare plans.

## 2. What exists today (survey, 11 Oct 2026)

| Piece | Where | What it does | Hub decision |
| --- | --- | --- | --- |
| Agent Quota Launcher | `github.com/alexeygrigorev/agent-quota-launcher`, Hetzner `~/git/agent-quota-launcher` | Stdlib Python CLI: `submit` (idempotent task), `plan` (dry-run admission), `run --backend task-units --as-controller` (fresh `quse` admission, resource gates, launch via `aplexer` in a transient systemd unit `ql-ctl-<id>.service`), lifecycle `queued→starting→running→completed-awaiting-review→accepted`, first-action `whoami` artifact, SQLite `state.db`. Providers: grok, antigravity (Gemini), zai (GLM). Codex unsupported in v0.1. | **Reuse as the only executor.** The host agent calls it; the hub never launches processes. Needs a `root-run` task profile (gap, see §11). |
| Quota gates | `cloudflare-agent-git/scripts/quota-gate.py`, `scripts/launch-codex.sh`, launcher admission | Fresh `quse` read, fail closed, 15% reserve. | **Reuse unchanged** on the host before every claim and inside the launcher. Heartbeats keep storing only the boolean `launch_allowed` + reason for scheduling; under the founder-authorized `QUOTA-HUB-SPEC-20261014` exception ([docs/QUOTA.md](QUOTA.md)), the host also posts one sanitized `quse --json` percentage/window snapshot every 600 s to `/api/quota` (no raw account, billing, credit or token balances). |
| Machine registry | `cloudflare-agent-git/coordination/MACHINES.json`, `scripts/machines.py`, `scripts/launch_gates.py --reachable-hosts` | Static list (Hetzner inbound-ssh; Win35 `DESKTOP-FP4OP26` and laptop `LAPTOP-PHA6TCNC` outbound-bus). Availability = ssh probe for Hetzner, bus check-in within 900 s for home machines; registration alone is not a check-in. | **Replace** with hub `machines` table + heartbeats. Keep the 900 s window and "registration is not a check-in" semantics. `machines.py available` gets a `--source hub` mode during migration. |
| Agent Bus adapter | Hetzner `agent-bus-win35-adapter.service` → `~/git/agent-bus/.local/wt-win35-endpoint-*/adapter/adapter.py` (source `github.com/PocketShell-io/agent-bus`), FileBus store `~/.local/share/agent-bus-win35/store`, port 8788, mTLS; `/v1/register,send,inbox,ack,accept,complete` | Home machines' bus. | **Replace** with hub `/api/messages`. Keep its verbs (send/inbox/ack/accept/complete) so clients port easily. |
| Agent Coordination candidate `8a919384` | `alexeygrigorev/agent-coordination`, Hetzner branch workspace `~/git/agent-coordination/.local/contest-head/branches/http-workspace`, commit `8a91938426287ba6ef592a5b750fb4a6dd66d569` "ac59: Repair deployed stdlib HTTP adapter for mTLS and structured payloads" (not on GitHub main) | Stdlib HTTP adapter for the AC59 coordination service. | **Reference only** for payload shapes and tests; do not depend on it. |
| Role authority / leases | `cloudflare-agent-git/scripts/recovery/role_fence*.py`, `host_recovery_adapter.py`; service `win35-role-authority.service` on :8801 (stood down, inactive) | Fenced election with `(project, role, actor, generation, epoch)`; every effect inside the election transaction. | **Port the model** (epoch fencing token, no cached authorization) into hub `leases`. Retire the service. |
| Desktop mailbox | `cloudflare-agent-git/scripts/orchestrator-channel-v2.py` running as aplexer shell session `desktop-orchestrator` on Hetzner; `.local/orchestrator-{inbox.json,outbox/,sent/,ack.json}` | Root's bound native aplexer identity; root reads/writes files over SSH. | **Replace** by the host agent's aplexer bridge (§7.5) + hub bus. |
| Supervision | `cloudflare-agent-git/scripts/supervision/service.py` in aplexer session `experiment-supervision`; reads `coordination/TEAM-REGISTRY.json`, `TASKS.json`, session list, launcher DBs every 60 s | Liveness and nudges. | **Replace** status part with hub health; registry import from TEAM-REGISTRY. |
| Principal ping | `cloudflare-agent-git/scripts/ping/principal-ping.{py,service,timer}` (systemd user timer on Hetzner, ~10 min) | Pings the principal; consecutive misses trigger recovery. | **Replace** with hub health + `principal-wake` job (M5). |
| Agent Dashboard | `github.com/alexeygrigorev/agent-dashboard`, Hetzner `~/git/agent-dashboard` | Python: hourly 24h utilization, usage accounting, completed features, localhost preview. | **Absorb the views.** Its accounting engine stays a host-side producer that posts summaries to `/api/metrics` (M6). |
| Prototype (Workers + Artifacts) | `cloudflare-agent-git/prototype/` (`wrangler.jsonc`, Durable Object `Coordinator`, SQLite class), `artifacts-spike/` | Agent Branches coordinator on Workers/DO; contest runs it on Node on Hetzner. | **Reuse patterns** (wrangler config, SQLite DO, vitest). No code dependency. |
| Agent Branches | `github.com/alexeygrigorev/agent-branches`, CLI `agent-branches`, Hetzner services `ab-contest-coordinator/sidecar` | The project's own code-hosting/coordination tool; `scripts/sync-main.sh` fast-forward push to GitHub. | **Develop this repo in Agent Branches**, sync to GitHub main. |
| DTC auth | `github.com/DataTalksClub/auth` (Hetzner `~/git/auth`), infra in `DataTalksClub/aws-infra` `sandbox/auth` | See §5.1. | **Use for humans.** Device flow built in the hub (Cognito has none). |

## 3. Platform

Cloudflare Workers **Free** plan:

- One Worker `agent-hub` (HTTP API + cron + static dashboard via Workers Static Assets).
- One SQLite-backed Durable Object class `Hub`, single global instance (`idFromName("hub")`). Single writer = strong consistency for leases, offers, the bus and the registry without D1 transactions. Free plan supports SQLite-backed DOs only, which is what we want.
- One Cron Trigger `*/30 * * * *` (the free plan allows 5 per account; the daily writer is scheduled from the same tick, so we use one). DO alarms for offer/lease timeouts.
- No KV, no D1, no R2, no Queues. No custom domain needed (`*.workers.dev`), unless Alexey prefers `hub.dtcdev.click` (DNS lives in DTC's AWS; decision).

Why not elsewhere: the project is Cloudflare-centric, budget is USD 5/month total, and the Workers Paid plan alone costs USD 5/month, so the design must fit the free tier with margin.

Free-tier constraints that shape the design: 100k Worker requests/day; 10 ms CPU per request and per cron invocation; 50 subrequests per invocation; DO 100k requests/day, 13,000 GB-s duration/day, 5M rows read/day, 100k rows written/day, 5 GB storage. Exceeding a limit makes calls fail (no surprise bill), so the hub must degrade gracefully and the host agent must back off.

Consequences:

- **No long-polls held inside the DO** (a held request bills DO duration: 3 hosts × 86,400 s × 0.128 GB ≈ 33,000 GB-s/day, over the limit). Use **Hibernatable WebSockets** (no duration while idle; incoming messages billed 20:1) as the primary host channel, and short HTTP polling (no waiting) as the fallback.
- Heartbeats are not written to SQLite on every beat: keep last-seen in DO memory and persist on state change or at most every 5 minutes per subject.
- Keep handlers cheap (ID token verification with WebCrypto is ~1 ms). No server-side rendering beyond small JSON; the dashboard is static JS calling the API.

### 3.1 Usage estimate (steady state)

| Source | Assumption | Worker req/day | DO req/day | Rows written/day |
| --- | --- | --- | --- | --- |
| Host WebSocket connects | 3 hosts, reconnect ~hourly | 72 | 72 | ~0 |
| Host heartbeats over WS | 3 hosts × 1/min | 0 | 216 (4,320 msgs ÷ 20) | ~900 (5-min persist) |
| Fallback polling (if WS down) | 1 host × every 30 s | 2,880 | 2,880 | ~300 |
| Agent bus traffic | ~15 actors, ~200 msgs/day each incl. inbox reads | 3,000 | 3,000 | ~3,000 |
| Health reports | 15 subjects, change-only + 5-min keepalive | 4,500 | 4,500 | ~4,500 |
| Root runs | 48 runs × ~10 API calls | 480 | 480 | ~500 |
| Dashboard | Alexey + root, ~500 views × 5 calls | 2,500 | 2,500 | 0 |
| Cron | 48 ticks | 48 | 48 | ~200 |
| **Total** | | **~13,500 (13% of 100k)** | **~14,000 (14%)** | **~9,500 (10% of 100k)** |

DO duration: ~14k requests × ~20 ms ≈ 280 s × 0.128 GB ≈ 36 GB-s/day (0.3% of 13,000). Storage: messages kept 30 days, health events 7 days: well under 100 MB.

**Cost: USD 0/month** on the Workers Free plan. Paid plan is not needed and would consume the whole USD 5 budget; any move to it requires Alexey. The DTC Cognito app client adds no meaningful AWS cost (Cognito free tier), but it lives in DTC's AWS account and needs Alexey's OK.

## 4. Components

1. **Hub Worker** (`src/worker.ts`): routing, auth (session cookie, bearer tokens), static assets, cron handler → forwards to the DO.
2. **Hub Durable Object** (`src/hub.ts`): SQLite schema (§6), scheduler, leases, offers, bus, health aggregation, WebSocket hibernation handlers, alarms.
3. **Host agent** (`host/agent_hub_host/`, Python ≥3.9 stdlib only, Linux + Windows): device login, register, heartbeat, local gate checks, job claim/run/renew/finish via the Agent Quota Launcher, local session health sampling via `a list --json`, aplexer mailbox bridge. Installed as a systemd user unit on Hetzner and as a Task Scheduler logon task on Windows. One per host.
4. **Hub CLI** (`hub`, same Python package): `hub login` (device flow), `hub send/inbox/show/ack/reply`, `hub claim/release`, `hub status`, `hub machines available`, `hub jobs`, `hub lease root`. Used by root runs, principals, heads and scripts.
5. **Job runners** (host side, thin wrappers over the launcher): `root-run`, `writer-run`, `head-recovery`, `principal-wake`, `custom`.
6. **Dashboard** (static assets under `public/`, plain JS, no framework needed).
7. **Deploy kit** (`scripts/deploy.sh`, `docs/DEPLOY.md`): prepared, not executed. Root asks Alexey before the first deploy.

## 5. Auth

### 5.1 What DTC auth provides (as found)

- `DataTalksClub/auth` README and `config/clients.json`: a shared Amazon Cognito user pool (`us-east-1`), hosted login at `https://auth.dtcdev.click`. Google is the only identity provider; a pre-signup/pre-auth Lambda (`src/index.py`, "domain guard") admits only Google identities with a **verified `@datatalks.club` email**, on first signup and every later login.
- Each relying service has its own Cognito **app client** with a registered callback/logout URL (dapier, dataops at `ops.dtcdev.click`, studio, datamailer, gym, and a public CLI client `dapier-cli` with loopback `http://localhost:8471/callback`). Infra (pool, clients, domain, DNS, IAM) is declarative in `DataTalksClub/aws-infra` `sandbox/auth`; `config/clients.json` is the tested public contract.
- Relying services do **authorization code + S256 PKCE**, validate the ID token (issuer `https://cognito-idp.us-east-1.amazonaws.com/<pool>`, `aud`=client id, `exp`, `nonce`, JWKS at `<issuer>/.well-known/jwks.json`) and create **their own** server-side session. Reference implementation: `DataTalksClub/dapier` `src/dapier/auth/dtc_auth.py` (env `AUTH_BASE_URL`, `AUTH_CLIENT_ID`, `AUTH_CALLBACK_URL`, `AUTH_ISSUER`, `AUTH_JWKS_URL`), `DataTalksClub/dataops` `backend/src/auth/browserAuth.ts` (TypeScript, closest to a Worker port).
- **No device authorization grant** (Cognito hosted UI has no RFC 8628), no machine/client-credentials identities, no groups/roles beyond the email domain. dapier solved device login by **brokering its own pairing flow** (`src/dapier/auth/device_sessions.py`, `api/agent/device.py`): `/device/start` mints a secret `device_code` + 8-char `user_code` (alphabet without 0/O/1/I/L), the operator approves on a `/device` page while signed in with DTC, the CLI polls `/device/token` and receives an app-issued opaque session token (`dapd_…`), stored hashed, with refresh and revoke.

### 5.2 Gaps and what we build

| Gap | Resolution |
| --- | --- |
| No app client for the hub | Request a new Cognito app client `agent-hub` (callback `https://<hub-host>/auth/callback`, logout `https://<hub-host>/`) via `DataTalksClub/aws-infra` `sandbox/auth` + an entry in `DataTalksClub/auth` `config/clients.json`. Prepared as a diff in `docs/DEPLOY.md`; needs Alexey (DTC AWS account). Local dev uses a fake OIDC issuer. |
| No device flow | Hub implements an RFC 8628-shaped broker modeled on dapier's (§5.4). |
| No authorization beyond domain | Hub allowlist `HUB_ADMINS` (emails, initially Alexey's `@datatalks.club` address) for human admin; all other DTC users denied by default. |
| No machine identities | Hub-issued opaque tokens bound to a machine or agent record with scopes. |

### 5.3 Human login

`GET /auth/login` → redirect to `https://auth.dtcdev.click/oauth2/authorize` with `response_type=code`, `client_id`, `redirect_uri`, `scope=openid email profile`, `state`, `nonce`, `code_challenge` (S256), `identity_provider=Google`. `state`/`nonce`/verifier kept in a short-lived signed cookie. `GET /auth/callback` exchanges the code at `/oauth2/token`, verifies the ID token (JWKS cached in DO memory, RS256 via WebCrypto), requires `email_verified=true` and email in `HUB_ADMINS`, then sets `hub_session` (HMAC-SHA256 signed, HttpOnly, Secure, SameSite=Lax, 12 h). State-changing browser calls require a CSRF header. `POST /auth/logout` clears it and redirects to Cognito logout.

### 5.4 Device login (machines and agents)

Shaped like RFC 8628 so standard clients work:

1. `POST /api/device/code` `{client_kind: "host"|"agent", name, hostname?, requested_scopes[]}` → `{device_code, user_code: "ABCD-EFGH", verification_uri: "/device", verification_uri_complete, expires_in: 600, interval: 5}`. Only `sha256(device_code)` is stored.
2. Human opens `/device` signed in through DTC, sees the requesting kind/name/hostname/IP-country/scopes, approves (optionally narrowing scopes, binding to an existing machine record) or denies.
3. Client polls `POST /api/device/token` `{grant_type: "urn:ietf:params:oauth:grant-type:device_code", device_code}` → `authorization_pending` | `slow_down` (polling faster than `interval`, interval +5 s) | `access_denied` | `expired_token` | success `{access_token: "hubt_…", refresh_token: "hubr_…", token_type: "Bearer", expires_in: 2592000, scope}`. A successful pairing is consumed exactly once.
4. `POST /api/token/refresh` rotates both tokens (old refresh token invalid immediately; reuse of a rotated refresh token revokes the family). `POST /api/token/revoke`. Admin can revoke any credential from the dashboard.

Scopes: `host` (register/heartbeat/claim jobs for its own machine), `bus:send`, `bus:read` (own actor inboxes), `health:report`, `jobs:create` (root/principal), `lease:root` (root runs only, combined with the lease epoch), `admin` (humans only, never issued via device flow). Tokens are stored as SHA-256 hashes; plaintext exists only in the pairing response. The host stores its token in `~/.config/agent-hub/host.json` (mode 600) or `%APPDATA%\agent-hub\host.json` (user-only ACL). Root runs and agents get a **short-lived job token** (scoped, expires with the job, minted by the hub at claim and passed by the host agent through the launcher environment), so agents never need their own device login.

## 6. Data model (Hub DO SQLite)

```
users(sub PK, email, is_admin, first_seen_at, last_seen_at)
credentials(id PK, kind host|agent|job|human, token_hash UNIQUE, refresh_hash UNIQUE, family_id,
            subject, machine_id NULL, actor NULL, scopes JSON, created_by_sub, created_at,
            expires_at, revoked_at NULL, last_used_at)
device_pairings(device_code_hash PK, user_code UNIQUE, client_kind, name, hostname, requested_scopes JSON,
            status pending|approved|denied|consumed|expired, approved_by_sub, granted_scopes JSON,
            machine_id NULL, interval_s, last_poll_at, created_at, expires_at)
machines(id PK, name, hostname, network public|home, os, roles JSON, capabilities JSON,
            credential_id, registered_at, last_checkin_at, last_heartbeat JSON, drained INT, notes)
            -- capabilities: {launcher: ver, engines: [...], jobs: ["root-run","writer-run",...],
            --                repos: {"cloudflare-agent-git": path}, aplexer: bool}
            -- last_heartbeat: {at, mem_avail_mb, disk_free_gb, load, gate:{launch_allowed, reason, at},
            --                  running_jobs:[...], sessions: n}
agents(tag PK, project, role root|principal|head|writer|worker|reviewer|service, machine_id,
            engine, session_id, workspace, team_id, imported_from, health_state, health_detail JSON,
            health_at, last_seen_at)
jobs(id PK (ulid), kind root-run|writer-run|head-recovery|principal-wake|custom, payload JSON,
            idempotency_key UNIQUE, requested_by, target_machine_id NULL, status
            queued|offered|claimed|running|succeeded|failed|expired|cancelled|no-host,
            offer_machine_id, offer_expires_at, attempts, lease_name NULL, lease_epoch NULL,
            launcher_task_id, session_ref, first_action_at, created_at, claimed_at, started_at,
            finished_at, deadline_at, result JSON)
offers(job_id, machine_id, offered_at, response accepted|declined|timeout, reason)   -- audit of random picks
leases(name PK, holder_job_id, holder_machine_id, holder_actor, epoch INT, acquired_at,
            renewed_at, expires_at, released_at, release_reason)
messages(id PK ulid, from_actor, to_actor, kind note|handoff|reply|status|claim|release, thread_id,
            reply_to, body TEXT (≤16 KB), idempotency_key, lease_epoch NULL, created_at,
            delivered_at, read_at, acked_at, accepted_at, completed_at, expires_at)
            UNIQUE(from_actor, idempotency_key)
claims(id PK, project, scope TEXT, holder, created_at, expires_at, released_at)
schedules(name PK, spec, tz, enabled, next_due_at, last_job_id, paused_reason)
health_events(id, subject_kind host|agent|job|hub, subject, state, detail JSON, at)   -- 7-day retention
metrics(name, project, bucket_start, value JSON, posted_by, posted_at)              -- dashboard summaries
audit(id, actor, action, target, detail JSON, at)                                    -- 90-day retention
```

Actor addresses: `tag` (unique project-wide tag such as `codex-principal`), `project:tag`, `role:principal`, `role:root` (current lease holder), `machine:<id>` (host agent). Unknown addresses are rejected, not silently queued.

## 7. Host agent protocol

### 7.1 Startup

1. Load token; if missing or revoked, run device login and print `user_code` + URL (a human approves once per host).
2. `POST /api/machines/register` with hostname, OS, network kind, roles, capabilities (launcher version from `python3 -m launcher --version` or git SHA, `aplexer` presence, engines from `quse`/config, repos it can run). The response is `{status:"registered", machine_id}` with the `machine_id` bound to the presented host credential (never taken from the body); the host persists it, which is how already paired hosts learn their id. Registration is **not** a check-in.
3. Open `GET /api/host/connect` (WebSocket, hibernatable). On failure, fall back to `POST /api/host/poll` every 30 s ± 20% jitter, exponential backoff on 5xx/1027 up to 10 min.

### 7.2 Heartbeat and sanitized quota snapshot (`QUOTA-HUB-SPEC-20261014`)

Every 60 s: `{type:"heartbeat", mem_avail_mb, disk_free_gb, load, gate, running_jobs, sessions_summary}`. `gate` is the result of the same gates a launch would use (`scripts/quota-gate.py` for Codex, the launcher's `plan` admission for other providers), cached on the host for at most 5 minutes; the heartbeat itself never carries quota balances, only `launch_allowed` + reason. A heartbeat is a check-in.

Separately from 60 s heartbeats, the founder-authorized `QUOTA-HUB-SPEC-20261014` contract ([docs/QUOTA.md](QUOTA.md)) permits each registered host to collect one bounded `quse --json` snapshot every 600 s (10 minutes) and post the sanitized percentage/window DTO (`schema_version: "1.0"`, max 32 KiB) to `POST /api/quota`, stored as a single latest row per verified host in `host_quota_snapshots` and read via authenticated `GET /api/quota` (marked stale at `age >= 1200 s`). Raw account identifiers, billing, credit/token balances, subscriptions, raw errors and transcripts remain strictly excluded, and quota snapshots never grant native task or Root authority. Consumers that read `/api/quota` and find it inaccessible, stale or in error may run exactly one fresh bounded local `quse --json` (≤ 30 s, stdout ≤ 64 KiB) and must then apply the same strict per-route and per-window gates. They fail closed on a missing route, a route not `ok`, unknown `limit_reached`, no present numeric window, or an unknown required window (an unknown Codex `5h` is never aliased to `7d`); see [docs/QUOTA.md §7](QUOTA.md#7-client-caching-and-fallback-contract).

### 7.3 Jobs

1. Hub sends `{type:"offer", job, offer_expires_at}` (or returns it from `poll`).
2. Host re-checks gates fresh, then `POST /api/jobs/{id}/claim` → `{job_token, lease_epoch?, deadline_at}` or `409` (someone else/lease held/offer expired). Decline with reason if gates fail.
3. Host submits and runs through the Agent Quota Launcher (`submit --id hub-<job id> --key <idempotency key> --payload …` then `run --backend task-units --as-controller …`) with the job token and `HUB_URL`, `HUB_JOB_ID`, `HUB_LEASE_EPOCH` in the environment.
4. `POST /api/jobs/{id}/start` with launcher task id, native session reference and first-action evidence (the launcher's first-action `whoami` artifact digest). A job without first-action evidence within 5 minutes is marked `failed: no first action`.
5. `POST /api/jobs/{id}/renew` every 5 minutes while running (renews the lease).
6. `POST /api/jobs/{id}/finish` `{status, summary (≤4 KB, sanitized), evidence_refs (private paths, not contents)}`. Exit 0 is not acceptance; the hub records the outcome, not a verdict.

### 7.4 Local health sampling

Every 60 s the host agent reads `a list --json` (and `a status <id> --json` for registered tags) and reports changes for agents the hub knows on that host: `working`, `waiting`, `idle`, `exited`, `broken`, `missing`. Reports only on change plus a 5-minute keepalive.

### 7.5 aplexer mailbox bridge (replaces desktop-orchestrator)

For tags registered on the host, the agent bridges hub messages to the native aplexer mailbox (`a message send --to TAG`) and native replies/ACKs back to the hub, so existing agents keep using `a message …` while root and other machines use the hub. The "synchronous reply" (typing into an idle session, `04-communication.md#keeping-heads-moving`) is performed only when the hub message asks for it **and** the host sees the target at an idle, empty prompt; never into busy, menu, draft or unknown states. The bridge never sets another agent's identity.

## 8. Scheduling and leases

### 8.1 Root run (every 30 minutes)

Cron tick → `Hub.tick(now)`:

1. Expire timed-out offers, claims and leases (`expires_at < now`) with a health event.
2. If lease `root` is held and its holder machine's last check-in is fresh: record `skip: root still running` and stop. **Only one root at a time**, enforced in the DO transaction, not by convention.
3. Create job `root-run` with idempotency key `root-run:<UTC slot start>` (a duplicate tick is a no-op).
4. Candidates = machines with check-in age ≤ 180 s (WebSocket connected or recent poll), not drained, `capabilities.jobs` contains `root-run`, last gate `launch_allowed=true`, and allowed by `ROOT_HOSTS` policy (default: all capable hosts; see open decisions). Exclude a host whose last two root runs failed (back off one slot).
5. Pick one uniformly at random (`crypto.getRandomValues`; tests inject a seeded RNG). Offer with a 120 s TTL; on decline/timeout, an alarm offers to the next random candidate, at most 3 hosts per slot. No candidate → job `no-host`, health alert, dashboard banner.
6. Claim acquires the lease atomically: `UPDATE leases SET holder…, epoch = epoch+1, expires_at = now+20min WHERE name='root' AND (holder_job_id IS NULL OR expires_at < now)`. The new `epoch` is the **fencing token**. Root's hub writes (messages as `role:root`, job creation, lease operations) must carry it; stale epochs are rejected.
7. Hard deadline 25 minutes, so a slot never overlaps the next; renewals every 5 minutes; release on finish. Lease expiry without finish → job `expired`, the next slot proceeds.

The root-run prompt is short: "You are root (headless hub run <job id>, epoch <n>). Read `cloudflare-agent-git/_docs/team/02-root.md`, `recovery/README.md`, `recovery/principal.md`, `recovery/head.md` and follow them, using the hub (`hub` CLI) for messages, health and job creation instead of SSH/file mailboxes. Finish within 20 minutes with a short sanitized report via `hub job finish`." The engine is chosen by the launcher's quota-aware ranking; the run must not start interactive sessions itself except via documented recovery bootstrap.

### 8.2 Writer (daily)

Schedule `writer-daily` (default 22:00 Europe/Berlin, configurable; decision for Alexey). The tick creates `writer-run:<Berlin date>` once per day, lease `writer:<date>`, only on hosts with `jobs` containing `writer-run` and the `cloudflare-agent-git` checkout (Hetzner today). The job runs the existing two-step flow: fact packet preparation (`.agents/skills/prepare-daily-journal/`, Codex) then `python3 website/write_daily.py --date <date>` (which keeps its own Claude quota check, launch lock and Opus verification). Publication (`publish_daily.py --publish`) stays with the existing coordinator; the hub never publishes or posts.

### 8.3 Head recovery and principal wake

`head-recovery` jobs are created by a root run or the principal (`jobs:create`), target the head's host, hold lease `head:<project>` (one recovery per head), and run `recovery/head.md` steps through the launcher. `principal-wake` (M5) replaces principal-ping: when the principal is `idle` with open hub messages for more than 15 minutes, the hub asks the principal's host agent to deliver one synchronous reply (idle empty prompt only).

## 9. Health model

Subjects: hub, hosts, root (lease + last run), principal(s), heads, writer, other registered agents.

| State | Meaning |
| --- | --- |
| `healthy` / `working` | Fresh evidence of activity |
| `idle` | Fresh, at an empty prompt |
| `waiting` | Fresh, blocked on input/menu (never auto-typed into) |
| `quota-held` | Host gate says no |
| `stale` | Host check-in 3–15 min old |
| `unavailable` | Host check-in > 15 min (900 s, as today) |
| `unknown` | No evidence. Never reported as dead: an SSH or check-in failure means unknown |
| `failed` | A job finished failed / no first action / lease expired |

Agents inherit `unknown` when their host is stale. Root runs are healthy when the last two slots produced a finish (or an explained skip). Alerts (dashboard banner + message to `role:principal` and the root-run inbox): no root run for 2 slots, principal not `working/idle` for 30 min, a head `broken/missing`, no host available, writer missed its day, hub near a free-tier limit (> 70% of any daily counter, tracked in DO memory and persisted hourly).

TEAM-REGISTRY import: `scripts/import-team-registry.py` reads `coordination/TEAM-REGISTRY.json` (teams → agents with tag, role, workspace, session_id) and upserts hub `agents`. After cutover the hub is the source and TEAM-REGISTRY becomes an export.

## 10. Dashboard

Static pages, DTC login required:

- `/` overview: root lease and last 6 runs, principal and heads health grid, host availability, alerts, free-tier usage gauges.
- `/machines`: registry, last heartbeat, gate state, capabilities, drain/undrain, revoke credential.
- `/jobs` and `/jobs/:id`: root, writer, recovery runs with offers (which hosts were picked and why), timeline, sanitized result.
- `/bus`: recent messages per actor (bodies visible to admins only), unacked counts, claims.
- `/health`: event timeline per subject.
- `/metrics`: summaries posted by agent-dashboard producers (hourly utilization, usage, accepted features).
- `/device`: approve a pairing.
- `/settings`: credentials, schedules (pause/resume root runs and writer), admins.

## 11. Gaps in existing tools the hub depends on

1. **Launcher profile for root/writer runs.** The launcher v0.1 supports grok/antigravity/zai only, rejects Codex, and has a 7200 s max timeout (fine). Needs a `root-run` profile (prompt file, no worktree, repo cwd, network) and a supported way to pass job env. File in `alexeygrigorev/agent-quota-launcher` and tell `quota-launcher-head`.
2. **Windows execution.** Whether Win35/laptop have aplexer + launcher + `quse` is unverified. Until a Windows host advertises `root-run`, root runs land on Hetzner only; the hub still registers and monitors home machines and carries their bus traffic.
3. **DTC app client** for the hub (Alexey / DTC AWS).
4. **Cloudflare account** for the deploy: which account, `workers.dev` subdomain, API token for `wrangler deploy` (Alexey).

## 12. Migration plan

| Phase | Change | Retired at end of phase |
| --- | --- | --- |
| P0 (local) | M0–M6 built and tested locally (`wrangler dev`, miniflare, and `wrangler deploy --dry-run`). No deploy. | — |
| P1 deploy (after Alexey's OK) | Deploy to Workers Free. Register Hetzner host agent. Import TEAM-REGISTRY. Hub runs **shadow** root runs: `root-run` jobs in `dry_run` mode (launcher `plan` only), proving selection, leases and health against reality. Existing Grok Bot routine continues. | — |
| P2 root cutover | Enable real root runs on Hetzner. Pause the Grok Bot 30-minute routine (root confirms with Alexey). Root runs use the hub bus; the Hetzner host agent bridges to aplexer mailboxes. | Grok Bot 30-min routine; desktop-orchestrator mailbox (after 3 days without use) |
| P3 home machines | Install host agent on Win35 and laptop (outbound WebSocket). Move their bus traffic from :8788 to the hub. `machines.py available` reads the hub. | `agent-bus-win35-adapter.service` (:8788) + FileBus store archived; MACHINES.json becomes a seed/export |
| P4 health + dashboard | Hub health and alerts replace supervision status; principal-wake replaces the ping timer; agent-dashboard posts metrics to the hub. | `principal-ping.timer`, `experiment-supervision` status role, agent-dashboard localhost preview |
| P5 writer | Writer runs from the hub schedule. | Any manual/other writer schedule (none duplicated) |
| P6 cleanup | role authority code archived (service already inactive). Docs in cloudflare-agent-git updated (`01-overview.md#machines`, `02-root.md`, `recovery/*`). | `win35-role-authority.service` unit file, role_fence runtime use |

Every retirement is done by the owning head/principal with an acknowledged handoff; nothing is deleted, only stopped and archived. Rollback for each phase = re-enable the retired piece; the hub never mutates their stores.

## 13. Milestones and acceptance tests

All tests run locally (vitest with `@cloudflare/vitest-pool-workers` or miniflare for the Worker/DO; `python3 -m unittest` for the host agent/CLI). No Cloudflare deploy, no paid resources, no real DTC login in tests (fake OIDC issuer with a test JWKS).

- **M0 Scaffold.** TypeScript Worker + `Hub` DO (SQLite), `wrangler.jsonc` (free-tier compatible, no deploy), Python package skeleton, `npm test`, `python3 -m unittest`. Accept: `/healthz` via `wrangler dev`; `wrangler deploy --dry-run --outdir dist` builds; tests green.
- **M1 Auth.** DTC login (PKCE S256, state, nonce, JWKS, `email_verified`, admin allowlist), session cookie, CSRF, device flow, token refresh/rotation/revoke, scopes. Accept: tests for wrong state/nonce/aud/iss/expired token, unverified or non-admin email denied, device flow pending/slow_down/approve/deny/expire/consume-once, refresh reuse revokes family, only hashes stored (assert on SQLite contents).
- **M2 Registry + host agent v1.** Register, heartbeat, availability (`GET /api/machines/available`), drain, WebSocket hibernation + poll fallback; Python host agent with device login. Accept: registration alone is not available; heartbeat makes it available; 900 s window; host agent integration test against `wrangler dev`; `machines.py`-compatible JSON output.
- **M3 Bus.** send/inbox/show/ack/accept/complete/reply, claims, idempotency, address validation, retention; `hub` CLI; aplexer bridge with a fake `a` binary. Accept: duplicate send with same key returns the original id; ACK only by recipient; unknown address rejected; bridge round-trip test; sync-reply refused when target not idle.
- **M4 Scheduler + root lease.** Cron tick, random selection (seeded), offers with TTL and fallback, claim race (two hosts concurrently → exactly one lease, epoch +1), renew/expire, epoch fencing on root writes, skip while active, `no-host`, idempotent slot. Host agent runs `root-run` via the launcher (fake launcher in tests; on Hetzner one real `launcher plan` dry-run recorded). Accept: all of the above as tests; a 24-hour simulated clock test shows 48 slots, never two concurrent leases.
- **M5 Writer, head recovery, principal wake.** Accept: one writer job per Berlin day across DST change; head-recovery lease per project; principal-wake only at idle.
- **M6 Health + dashboard.** Health states and alerts, TEAM-REGISTRY import, metrics endpoint, dashboard pages. Accept: state-transition tests; free-tier counter alert at 70%; dashboard pages render against local data (screenshot artifacts by the reviewer).
- **M7 Deploy kit.** `scripts/deploy.sh` (refuses without explicit `--i-have-approval`), `docs/DEPLOY.md` (Cloudflare account steps, secrets list, DTC client diff for `aws-infra`/`auth`, host agent install for Linux/Windows, rollback), usage-estimate check script. Accept: dry run passes; a different-model reviewer walks the runbook. Then root asks Alexey for the deploy.

Each milestone: implementer(s) and a reviewer on a different model via the Agent Quota Launcher, accepted result integrated to GitHub main, milestone comment on the tracking issue.

## 14. Open decisions for Alexey

1. Approve the first deploy to Cloudflare Workers Free (which account; `workers.dev` or `hub.dtcdev.click`).
2. Approve a new Cognito app client `agent-hub` in DTC auth (DTC AWS account), and confirm the admin email(s).
3. Root-run host policy: any capable host, or Hetzner-only until home machines prove out (current docs forbid the old Win35 root; a hub-launched headless root on Win35 is a different thing but should be explicitly allowed).
4. Writer schedule time (default 22:00 Berlin) and whether the 09:00 standup check also moves to the hub.
5. When to pause the Grok Bot 30-minute routine (proposed: start of P2).
