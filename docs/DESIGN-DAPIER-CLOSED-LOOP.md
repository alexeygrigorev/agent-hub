# Design: Hub ↔ Dapier closed-loop task events, sticky-session role claims, cross-machine messaging and the Desktop link

Intake `93a5c88d-aff6-4eb3-ad00-2ad088e0d501` (parent `408b585e-2ecb-4b3a-8d23-db09455a24da`).
Status: design/contract, no code. Implementation slicing targets the sibling
sessions: `task-hub2-6d932042` (agent-hub) and `b697ad99` (dapier).

Alexey's ask, verbatim intent: every Hub task start/complete/finish/fail should
emit a webhook integrated into Dapier, and Dapier messages back to Dev's sticky
session ("this task is finished / failed") so Dev knows without polling. When
Alexey tells a fresh session "you're tofu / you're dev / you're orin", the
session must claim that role on Hub, detect and resolve conflicts (takeover),
and update routing so role messages and webhooks reach the active sticky
session. Role mail should fall back from the quota-broken Grok bots to sticky
sessions. Everything rides the existing cross-machine message hub and a
reliable Hub↔Desktop connection that never exposes Desktop to the internet.

## 0. Participants, existing building blocks, non-goals

| Actor | Where it runs | Role in this design |
| --- | --- | --- |
| Hub2 | hub VM, systemd `agent-hub-hub2.service`, SQLite, caddy edge `https://hub.dtcdev.click` | Authority for roles, task lifecycle events, message hub, quota export |
| Dapier | AWS serverless (API Gateway → Lambda → SQS → workflow worker, DynamoDB) | Receives lifecycle webhooks; routes role mail; sends messages back into Hub |
| Desktop | `DESKTOP-FP4OP26`, home network, no inbound SSH/ports | Runs enrolled sticky sessions (Orin needs Desktop) |
| Sticky session | Interactive engine session bound to a machine, holds a role | Receives "task finished / failed" and "check your emails" pushes |
| Grok bots (Dev/Tofu/Orin) | External, quota-limited | Default mail destination; fallback target when quota < 1% |

Building blocks that already exist and are reused unchanged:

- **MailboxDO** (`hub2/src/mailbox-do.ts`) — the single global message
  authority: `POST /api/messages` (idempotency via `UNIQUE(sender,key)`,
  body ≤ 12 000 chars), receipt stages `read/ack/accept/complete`, native
  `delivered` receipts, `native_outbox` with states
  `unrouted → pending-machine → queued → delivered`, and a **`readdress`**
  endpoint for redirecting undelivered mail. Directory resolution goes through
  the CONTROL DO `_recipient` route returning `{mailbox, forward_session?}`.
  The host daemon already transports these: `hub2/host/agent_hub_host/messages.py`
  posts `/api/messages/<id>/delivered`.
- **Verified session send path** — durable session-control `send` commands +
  ack ledger; `hub sessions wake <session> --at <ISO|now> (--prompt | --body-file)`
  with the 10-minute ack window and `needs_attention` on miss.
- **Host daemon transport** (`hub2/host/agent_hub_host/transport.py`,
  `agent.py` bridge + journal) — outbound-only WebSocket to the hub edge
  (pinned `websockets==16.0`, Bearer enrollment token), journal-backed
  enqueue/flush for durability, heartbeat loop. This is the Desktop link; it
  requires zero inbound ports.
- **Intake lifecycle** (`hub2/src/project-do.ts`) — `intake_events` durable
  event log (`created`, `start:<op>:requested`, `finish:<op>`,
  `worker_append`, `developer-owner:*`, blocked/completion records) and intake
  statuses `open → queued → running → completed-awaiting-review → done`
  (+ `archived`); `developer_alert_raised` mail-to-dev@ poller precedent for
  outbound operator notification.
- **Dapier developer webhook** (`~/git/dapier/docs/developer-webhook.md`) —
  `POST https://dapier.dtcdev.click/hooks/developer` accepting
  `X-Dapier-Signature: sha256=<hex>` (HMAC-SHA256 of the raw body) or
  `Authorization: Bearer`; SES routes `dev@/tofu@/orin@dtcdev.click` →
  EventQueue → `<role>-mailbox` workflows → Grok bot webhooks.

Non-goals: no Cloudflare/wrangler, no new paid services, no inbound tunnels or
port-forwarding to Desktop, no changes to retired Hub1 services, no Grok bot
replacement (Dapier keeps routing to them as the default destination).

---

## 1. Sticky sessions and the role-claim / takeover protocol (Hub2)

### 1.1 Concepts

- **Role**: a lowercase slug from a fixed allowlist `dev`, `tofu`, `orin`
  (extensible only by config, never free-form). Roles are routing addresses:
  mail, lifecycle notifications and messages addressed to a role must land in
  the session that currently holds the role.
- **Sticky session**: an interactive Hub session bound to a specific machine
  (Desktop for this first iteration) that holds at most one role and keeps the
  role alive via its existing session liveness heartbeat.
- **Fencing epoch**: a monotonically increasing integer per role, incremented
  on every claim grant or takeover. Holders must present their epoch when
  acting for the role; every routing decision stamps the epoch it resolved so
  stale holders cannot silently consume deliveries.

### 1.2 Storage (Hub2, CONTROL DO — the global authority)

One table beside the existing control tables:

```sql
CREATE TABLE roles(
  role TEXT PRIMARY KEY,            -- 'dev' | 'tofu' | 'orin'
  holder_session_id TEXT,           -- NULL when free
  holder_machine_id TEXT,           -- binding of the sticky session
  holder_actor TEXT,                -- 'session:<uuid>'
  epoch INTEGER NOT NULL,           -- fencing token, starts 1
  claim_note TEXT,                  -- human reason, e.g. 'alexey said "you are tofu"'
  claimed_at INTEGER,               -- unix seconds
  lease_expires_at INTEGER,         -- unix seconds; NULL when free
  takeover TEXT,                    -- NULL | 'pending' | 'superseded' (old-holder state)
  takeover_prev TEXT,               -- json {session_id, epoch, notified_at, acked_at}
  updated_at INTEGER NOT NULL
) WITHOUT ROWID;
```

Lease: `lease_expires_at = now + ROLE_LEASE_TTL` (default 30 min, config in
`resource_settings`-style validated settings; fail closed when missing/invalid).
Sticky sessions renew implicitly: every session liveness heartbeat that arrives
from the holder extends the lease; no new heartbeat endpoint is introduced.

### 1.3 API contract (Hub2, operator/session auth via existing control-auth)

All routes JSON; all mutation routes are idempotency-guarded by
`UNIQUE(role, request_key)` in a small `role_claims` ledger so retries never
double-transfer.

**`GET /api/roles`** → `{roles:[{role, holder_session_id, holder_machine_id, epoch, claimed_at, lease_expires_at, lease_stale:boolean}]}`. Public to any authenticated actor (routing consumers need it).

**`GET /api/roles/{role}`** → single row as above, plus `takeover` state when pending. `404 role_unknown` outside the allowlist.

**`POST /api/roles/{role}/claim`**

```json
// request (actor: the claiming session itself, or operator:admin)
{"session_id":"<uuid>","request_key":"<opaque, e.g. uuid>",
 "takeover":false,"reason":"alexey said: you are tofu"}
```

Responses:

- `200 {"status":"granted","role":"tofu","epoch":7,"lease_expires_at":...}`
  — role was free (or already held by *this same session*: re-grant, epoch
  unchanged, lease extended).
- `409 {"status":"held","role":"tofu","holder":{"session_id":"...","machine_id":"...","epoch":6,"claimed_at":...,"lease_stale":false}}`
  — another live session holds it. **No silent takeover**: with
  `takeover:false` this is a conflict answer the session reports to Alexey.
- `409 {"status":"stale_lease"}` variant with `holder` attached when
  `lease_expires_at < now`: the holder is dead/stale; the claim is auto-granted
  after the server releases the stale holder (see 1.5), so the client simply
  retries once and gets `granted`.
- `403 claim_forbidden` when the actor is not the claimed session / operator.
- `400 invalid_role|invalid_session|request_key_required`.

**`POST /api/roles/{role}/claim` with `"takeover":true`** — explicit
takeover. The server, in one transaction:

1. Records `takeover='pending'` and `takeover_prev={session_id, epoch,
   notified_at}` for the old holder, grants the role to the new session,
   `epoch += 1`, sets the new lease.
2. Sends the old holder a mailbox message (sender `hub:roles`, recipient =
   old holder actor, key `role-revoked:<role>:<epoch>`):

   ```json
   {"kind":"role_revoked","role":"tofu","epoch":6,
    "new_holder":{"session_id":"...","machine_id":"...","reason":"alexey said: you are tofu"},
    "deadline_epoch":6}
   ```

3. The old holder has a **10-minute ack window** (same semantics as the wake
   ack window) to `POST /api/messages/<id>/ack`. Ack → clean handoff; expiry →
   `takeover='superseded'` + `needs_attention` note on the old session
   (operator visibility), no rollback — the new holder keeps the role.

**`DELETE /api/roles/{role}/claim`** (holder or operator:admin) → release; role
becomes free, epoch unchanged. A session that exits without releasing is
handled by lease staleness (1.5).

**`POST /api/roles/{role}/renew`** — explicit renew for sessions that want to
hold a role while otherwise quiet; same auth as claim; returns the same
`granted` shape. Optional; heartbeats already cover the normal case.

CLI surface (operator + in-session): `hub roles list`, `hub roles show <role>`,
`hub roles claim <role> --session <id> [--takeover] --reason "..."`,
`hub roles release <role>`. MCP parity: `roles_list`, `roles_claim`,
`roles_release`.

### 1.4 Session-side protocol (what happens when Alexey says "you're tofu")

1. Session parses the intent (or Alexey runs `hub roles claim tofu --session <me>`).
2. `POST /api/roles/tofu/claim {session_id, request_key:uuid, takeover:false, reason}`.
3. `granted` → session confirms "I am tofu (role held, epoch 7)"; done.
4. `409 held` with `lease_stale:false` → session must **not** self-takeover
   silently; it reports: "tofu is held by session X on machine Y since ….
   Say 'take over tofu' to force." On explicit confirmation it re-claims with
   `takeover:true`; the old holder gets the revocation message (1.3) and the
   routing epoch moves.
5. On graceful exit the session releases its role; the watcher `--body-file`
   summary path already covers "session ended" notification.

### 1.5 Edge cases (role protocol)

| Case | Behavior |
| --- | --- |
| Two sessions claim concurrently | CONTROL DO is the single serialized authority; one `granted`, one `409 held`. No split-brain. |
| Holder crashes / session dies | Lease expires after TTL; next claim sees `lease_stale` and auto-supersedes; the dead holder's pending role mail is readdressed (2.4). |
| Holder alive but idle past TTL | Session heartbeats renew the lease; if a session goes quiet > TTL it loses the role and the takeover message goes to it; if it later acts for the role with a stale epoch it is fenced (409 `epoch_mismatch`). |
| Same session re-claims | Idempotent re-grant (same epoch, lease extended) — safe retries. |
| Claim while a takeover is pending | Second takeover request supersedes the first pending takeover atomically (last confirmed intent wins; the first new holder gets a revocation message too). |
| Role never claimed | Routing consumers get `holder:null`; Dapier falls back (2.3). |
| Operator wants to clear a stuck role | `hub roles release <role> --operator` works from any machine; audited. |
| Allowlist growth (new role) | Config change only; unknown roles always 404 — no free-form address squatting. |

---

## 2. Hub task lifecycle webhooks → Dapier → closed loop back to Dev

### 2.1 Event source and outbox (Hub2)

Lifecycle events are derived from `intake_events`/intake status transitions,
never from process exit. ProjectDO appends a row to a dedicated durable
outbox in the same transaction as the state change:

```sql
CREATE TABLE lifecycle_webhook_outbox(
  event_key TEXT PRIMARY KEY,     -- '<intake_id>:<kind>:<seq>' — replay-safe
  intake_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- started | completed | finished | failed
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,            -- pending | delivered | failed | dead
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt INTEGER,           -- unix seconds; backoff 1→5→15 min
  last_error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX lifecycle_due ON lifecycle_webhook_outbox(state,next_attempt) WHERE state='pending';
```

Event kinds (exactly four, mapped from existing states):

| kind | Fires when |
| --- | --- |
| `started` | dispatch saga moves the intake to `running` (session actually started on a machine) |
| `completed` | the session's completion record is accepted (`task_complete` covering every acceptance criterion, `completed-awaiting-review`) |
| `finished` | intake reaches `done` (acceptance verification / merge-check passed — the point Alexey cares about) |
| `failed` | a `task_blocked` record is filed, recovery is exhausted / `needs_attention` raised, or the intake closes without completion |

### 2.2 Delivery contract Hub → Dapier

```
POST https://dapier.dtcdev.click/hooks/hub-task
X-Hub-Signature: sha256=<hex>            # HMAC-SHA256(secret, raw body) — same scheme dapier already verifies as X-Dapier-Signature
X-Hub-Event-Key: <event_key>             # convenience duplicate of the body field
Content-Type: application/json
```

Secret lives in Dapier Secrets Manager (e.g.
`HUB_LIFECYCLE_WEBHOOK_SECRET_NAME`), mirrored in Hub's credential store —
never in Git, issues or bus messages. Dapier may alternatively accept
`Authorization: Bearer <secret>` — its existing `/hooks/developer` already
supports both; `hub-task` reuses that exact verification code path.

Payload (≤ 16 KiB, titles/summaries only — no secrets, no private evidence,
payload bodies are exactly what may appear on the tracking issue):

```json
{
  "event_key": "408b585e-...:finished:42",
  "kind": "finished",
  "project": "agent-hub",
  "hub_task_id": "93a5c88d-aff6-4eb3-ad00-2ad088e0d501",
  "title": "Design pass: Hub->Dapier closed-loop task webhooks ...",
  "status": "finished",
  "session_id": "fcb32972-...",
  "machine_id": "hetzner-aplexer",
  "attempt": 0,
  "summary": "PR #533 merged; all acceptance criteria met",
  "failure_cause": null,
  "source": {"type": "hub", "reference": "https://hub.dtcdev.click/intakes/93a5c88d-...", "timestamp": "2026-11-19T12:00:00Z"}
}
```

`failure_cause` uses the existing blocked vocabulary
(`permission|credential|external|waiting|other`), `null` otherwise. Unknown
fields may be added by Hub later; Dapier must ignore unknown fields.

Delivery semantics: at-least-once. Dispatcher runs on the existing 5-minute
monitor cron plus a DO alarm for freshness; backoff 1→5→15 min, max ~24 h,
then `dead` and a `developer_alert_raised`-style mail to `dev@` (existing
poller precedent). Dapier dedupes on `event_key` (SQS + DynamoDB idempotency
record, the pattern it already uses) and returns `202` on accept, `4xx`
(non-retryable, marked `failed`), `5xx` (retry). Replay/retest:
`hub lifecycle events list [--status pending|dead]` and
`hub lifecycle events replay <event_key>` (operator only) — no secret-bearing
output.

### 2.3 Closed loop: Dapier → Dev's sticky session

Dapier's `hub-task-events` workflow (new; same shape as `dev-mailbox`)
receives the webhook and resolves the destination for the target role
(default `dev`; the event carries no role so the workflow config holds the
mapping `finished/failed → dev`):

1. `GET https://hub.dtcdev.click/api/roles/dev` (Bearer = dapier API actor,
   least-privilege `AGENT_ACTIONS`-style scope — dapier's existing agent-gate
   pattern).
2. If `holder_session_id` is set and `lease_stale=false` → **deliver into the
   sticky session** via the mailbox (this is the "closed loop", and it is also
   the first production consumer of the cross-machine message hub):

   ```
   POST https://hub.dtcdev.click/api/messages
   Authorization: Bearer <dapier mailbox actor token>
   {"recipient":"role:dev","key":"<event_key>",
    "body":"Hub task finished: <title> (<project>).\n<summary>\n<reference>"}
   ```

   `key=<event_key>` makes Hub→session delivery idempotent under Dapier
   retries. The mailbox directory resolves `role:dev` to the holder (2.4) and
   the native outbox pushes it to the holder's machine; the session sees it on
   its next turn/wake. **Ack contract**: the sticky session acks the message
   (receipt `ack`) within the same 10-minute window used by wakes; the
   workflow polls `GET /api/messages/<id>/status` up to 10 min. Delivered +
   acked = loop closed; missed ack → Dapier raises its existing error surface
   (workflow error + DLQ alarm) and Hub's stall detection covers the session
   side.
3. If the role is unheld/stale → fall back to the current behavior: the Grok
   bot webhook (default destination) or, for mail events with quota < 1%
   (parent AC1), the sticky-session fallback decided there. Lifecycle events
   are *never* dropped silently: fallback delivery is recorded on the workflow
   run either way.

No polling by Dev anywhere: pushes go session-ward (2), and Dapier→Hub state
checks are event-driven workflow steps, not timers.

### 2.4 Role-aware routing inside Hub (the glue)

- **Mailbox directory**: `_recipient` is extended: recipient `role:<name>`
  resolves through the `roles` table to `{mailbox: holder_actor,
  forward_session: holder_session_id, epoch}`. Unknown/unheld role → `404`
  (Dapier falls back per 2.3). The resolved epoch is stamped on the
  `native_outbox` row.
- **Takeover readdress**: on any claim grant that changes the holder, Hub
  calls the existing mailbox `readdress` for all `role:<name>` messages not
  yet `delivered`, moving them to the new holder (this is exactly what
  `readdress_operations` exists for). Messages already delivered stay where
  they are — the old holder was told via `role_revoked`.
- **Webhook routes**: `target_label` gains the `role:<name>` form (resolved at
  delivery time like the existing session labels; zero matches stay queued,
  ambiguity impossible because roles are unique). This lets the existing
  inbound webhook machinery (Dapier mail → Hub) target roles without code
  changes on Dapier's side.
- **Wakes**: `hub sessions wake` keeps targeting explicit sessions; role
  addressing is intentionally mailbox-first so receipts/readdress stay
  uniform.

### 2.5 Quota export contract (consumed from sibling intake 408b585e AC1)

Dapier's mail fallback needs GrokBot remaining usage; the contract it builds
against (implementation owned by `6d932042`):

```
GET https://hub.dtcdev.click/api/quota/grokbot
→ 200 {"provider":"grokbot","remaining_percent":12.4,"window_start":"...","window_end":"...",
       "updated_at":"...","stale":false}
→ 503 {"error":"quota_unavailable"}   # exporter has no fresh sample
```

`stale=true` when `updated_at` older than 30 min. Dapier rule: forward mail to
the sticky session when `remaining_percent < 1.0` **or** the endpoint answers
`503`/`stale=true` for > 2 consecutive polls (fail-open toward Hub, since
Grok-bot delivery is the thing that is broken); otherwise the default Grok
webhook. If the role is also unheld, mail still goes to the Grok webhook (last
resort) and the run records the degraded routing.

### 2.6 Edge cases (lifecycle + loop)

| Case | Behavior |
| --- | --- |
| Dapier retries a webhook | `event_key` idempotency on both sides (Hub outbox PK; Dapier DynamoDB record). No duplicate session messages. |
| Hub webhook target down for hours | Outbox retries with backoff, then `dead` + dev@ alert; events are never lost, replay via CLI. |
| Sticky session dies between resolve and deliver | Directory resolution stamps epoch; native outbox sees a stale/unbound target → stays `pending-machine`, takeover readdress (2.4) moves it, or lease expiry makes the next claim readdress it. |
| Two lifecycle events in flight (`completed` then `finished`) | Independent outbox rows; ordering per intake is guaranteed by `seq` in `event_key` and FIFO dispatch per intake (single dispatcher, ORDER BY created_at). |
| Dapier workflow errors | SQS retry + DLQ + existing CloudWatch alarms; the event_key dedupe means redrives are safe. |
| Message body over limits | Hub truncates summary fields at emit time (16 KiB payload cap); mailbox bodies cap at 12 000 chars — workflow renders within that. |
| Secret rotation | New secret → both sides update; verification window: accept old+new for 24 h on Dapier (standard dual-secret pattern), then drop old. |

---

## 3. Cross-machine message hub + reliable Hub↔Desktop link

### 3.1 Message hub (exists; this design makes it load-bearing and tested)

MailboxDO stays the single authority. What this design adds:

- Role addressing (2.4) as the first real consumer — `role:dev` mail to a
  Desktop sticky session is the canonical end-to-end path.
- **Acceptance test plan** (goes with implementation): (a) operator on Hetzner
  sends to `role:dev` with a sticky session live on Desktop → message shows
  `delivered` + session displays it; (b) receipts walk `read→ack→complete`;
  (c) kill the Desktop daemon mid-flight → message parks in
  `pending-machine`, daemon restart → delivered; (d) takeover mid-flight →
  readdress lands it in the new holder; (e) duplicate send with same key →
  replay, no second row.
- Sender identities: machines/sessions already act via control-auth; Dapier
  gets a dedicated least-privilege actor (mailbox-send scope only, per the
  dapier agent-directory pattern). No shared human tokens in workflows.

### 3.2 Reliable Hub↔Desktop connection (no inbound exposure)

Desktop enrolls as a normal Hub2 machine (existing enrollment, HUB2-HOST
release env pinned `websockets==16.0`) and runs the existing host daemon:

```text
Desktop daemon ──outbound WSS 443──▶ caddy edge (hub.dtcdev.click) ──▶ hub2 node
   ▲                                       │
   └──── journal flush / offers / message delivery, heartbeat loop ────┘
```

- **Only outbound TLS from Desktop** — the daemon initiates the WebSocket
  (`transport.py`), authenticated with the enrollment Bearer token. Home
  network keeps NAT/firewall closed; no SSH exposure, no port forwarding, no
  tunnel vendor, no new paid service. Hub→Desktop is *enqueue then pull*: hub
  writes commands/messages durably (SQLite + outbox), the daemon's connection
  loop fetches them — inbound reachability of Desktop is never required.
- **Reliability mechanics** (all existing, with tuned parameters called out):
  reconnect with jittered exponential backoff; journal-backed
  enqueue/flush so a flapped connection never loses an ack or receipt;
  heartbeat loop keeps session liveness fresh; hub-side deadlines
  (`deadline_ms` on machine commands) and `next_attempt` on the
  native outbox redeliver; missed heartbeats feed the existing
  `needs_attention`/developer-alert path.
- **SLO + alerting contract**: heartbeat absence > 15 min on an enrolled,
  expected-online machine raises a developer alert naming the machine; role
  leases on that machine show `lease_stale` (1.5) and Dapier falls back
  (2.3/2.5). Desktop asleep is therefore *visible and routed around*, not
  silent loss.
- **Failure matrix**:

| Failure | Effect | Recovery |
| --- | --- | --- |
| Hub down / redeploy | Daemon reconnect loop journals locally; nothing sent | Messages queued hub-side deliver on reconnect |
| Desktop network flap | WSS drops mid-ack | Journal replays unacked frames; outbox `next_attempt` redelivers |
| Desktop asleep/off | No heartbeats; lease goes stale | Mail/webhooks fall back to Grok per 2.3; alert after 15 min; role claimable |
| Daemon crash | systemd restarts (release unit) | Re-fetch pending commands; idempotency keys prevent duplicates |
| Enrollment token revoked | 401 on reconnect; alert | Re-enroll via existing operator flow |

### 3.3 What "reliable" means here (acceptance)

1. Mail `dev@dtcdev.click` while Grok quota < 1% and a Dev sticky session is
   live on Desktop → "hey, check your emails" lands in that session within
   ~1 min, with `ack` receipt.
2. `hub lifecycle events` shows a `finished` event delivered to Dapier
   (`202`), and Dapier's workflow run shows the session message delivered+acked.
3. Takeover drill: start a second session, "you're dev, take over" → old
   session receives `role_revoked`, in-flight role mail readdresses, new
   session receives subsequent mail.
4. Desktop offline drill: no holder → mail falls back to Grok webhook; alert
   fires; on return, claim + routing recover without operator DNS/firewall
   changes.

---

## 4. Implementation slicing

**agent-hub (`6d932042` and follow-ups)**: `roles` table + claim/takeover APIs
+ CLI/MCP; mailbox directory `role:` resolution + takeover readdress;
`lifecycle_webhook_outbox` + dispatcher + `hub lifecycle events` CLI; quota
export endpoint; Desktop enrollment + daemon unit + alert tuning; tests
(unit + the 3.3 drills in CI E2E where the harness permits).

**dapier (`b697ad99`)**: `hub-task` webhook route reusing `/hooks/developer`
verification; `hub-task-events` workflow (resolve role → mailbox send → ack
poll → fallback); mail-fallback rule consuming `GET /api/quota/grokbot`;
dapier Hub actor credential; run-record fields for degraded routing.

**Sequencing**: roles + directory first (everything routes through it), then
outbox/webhooks, then Dapier workflows, then quota fallback flip. The Grok
webhooks remain the default destination until the sticky-session path proves
out in the 3.3 drills — no cutover before that.

## 5. Open questions (default answers chosen; Alexey can override)

1. Should `completed` (claim accepted) and `finished` (verified done) both
   notify Dev, or only `finished`/`failed`? Default: notify on all four; the
   workflow filters per role if noisy.
2. Lease TTL 30 min: long enough for an interactive session that thinks between
   turns, short enough for same-day takeover. Default 30 min.
3. Role allowlist starts `dev|tofu|orin`. Default fixed via config; new roles
   need a config change (prevents address squatting).
4. Orin's Desktop requirement: Orin claims are accepted on any machine, but
   routing fallback for `orin` should prefer a Desktop-bound holder
   (`holder_machine_id` preference in Dapier config). Default: prefer Desktop,
   accept others with a degraded marker.
