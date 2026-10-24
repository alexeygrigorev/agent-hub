# Independent Argon Source Review — M12a Private Intake Ledger, API, Public CLI & Durable Handoff Context (`c290454` / `adae0fb`)

**SCOPED ACCEPT — exact composed commit `c290454e88c49c6fe863d20dd2717d58522851d0` (merging M12a author commit `adae0fb346028c449db7ce6b5858720523ffd6a9` onto current Hub `main` `037bf039ec5ae01799a1cc863d31178a76107cdf`), for the M12a private intake ledger, `/api/intake/*` HTTP API, public `hub intake` / `hub handoff` CLI, and durable handoff context increment only.**

- **Reviewer**: `platform-intake-review` / `gemini-4-argon-high` (native session `2177ea2b-3547-4fb9-855b-bad049381dfc`, isolated worktree `/home/alexey/git/agent-hub-wt-platform-intake-review` at exact `c290454e88c49c6fe863d20dd2717d58522851d0`)
- **Author of Reviewed Source (`adae0fb` / `df80625`)**: `platform-intake` / `gpt-6.1-sol` (Codex session `df173430`, distinct identity and model family from this reviewer)
- **Author of Composed Merge Pin (`c290454`)**: `platform` / `codex` (session `e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363` in `/home/alexey/git/agent-coordination`)
- **Reporting To**: `platform` (`/home/alexey/git/agent-coordination`)
- **Date**: `2026-10-17`

Review: approved sha=c290454e88c49c6fe863d20dd2717d58522851d0 reviewer=platform-intake-review/gemini-4-argon-high
Review: approved sha=adae0fb346028c449db7ce6b5858720523ffd6a9 reviewer=platform-intake-review/gemini-4-argon-high

---

## 1. Executive Verdict, Pinned Lineage & Explicit Scope Boundaries

### 1.1 Verdict Summary

Exact composed source commit `c290454e88c49c6fe863d20dd2717d58522851d0` (which cleanly merges author branch commit `adae0fb346028c449db7ce6b5858720523ffd6a9` onto `origin/main` `037bf039ec5ae01799a1cc863d31178a76107cdf` with all nine M12a author-owned files byte-identical to `adae0fb346028c449db7ce6b5858720523ffd6a9`) satisfies all source-level requirements and binding implementation contracts for the **M12a intake ledger, HTTP API, public CLI (`hub intake` / `hub handoff`), and durable orchestrator handoff context** specified in:

1. Alexey's 17 October 2026 intake brief and both addenda (`/home/alexey/tmp/agent-reports/hub-intake-brief.md`);
2. Coordination plan commit `27e4b32487eb556cab624dfcc638cfa43faa0dd1` (`docs/platform/PLAN.md` `L325–509`, including the 7-point contract details at `L428–501`); and
3. The baseline Argon plan review `docs/reviews/INTAKE-TASKS-PLAN.md` (`a13d227c6486221b5fb5f8b31948648cdea1f20d` on `037bf039ec5ae01799a1cc863d31178a76107cdf`, Gates `M12-IG1`, `M12-IG3`, `M12-IG4`, and the ledger/provenance boundary of `M12-IG6`).

All 36 author Worker/SQLite HTTP tests (`test/intake.test.ts`), all 15 author Python stdlib/public CLI tests (`host/tests/test_intake.py`), `tsc --noEmit`, `git diff --check`, and 14 additional reviewer-authored adversarial probes (10 Worker/SQLite HTTP probes in `.local/review/intake-adversarial.probe.test.ts` and 4 Python CLI + unmocked `_TokenExchange` host-binding composition probes in `.local/review/test_intake_adversarial.py`) pass on `c290454e88c49c6fe863d20dd2717d58522851d0`. Zero blocking or non-blocking source defects were found in this scoped increment.

### 1.2 Explicit Phase Boundaries & Remaining Open M12 / Roadmap Gates

This **SCOPED ACCEPT** covers **only** the M12a durable intake ledger, `/api/intake/*` HTTP endpoints, public `hub intake` / `hub handoff` CLI, and source-level handoff continuation fixture at `c290454e88c49c6fe863d20dd2717d58522851d0`. It does **not** close full `M12` and does **not** claim implementation or acceptance of the remaining M12 phases:

| M12 Phase / Gate | Status at `c290454e88c49c6fe863d20dd2717d58522851d0` | What Remains Required for Full Closure |
| :--- | :--- | :--- |
| **M12a — Task API, Intake Ledger & Durable Handoff Store** (`M12-IG1`, `M12-IG3`, `M12-IG4`) | **ACCEPTED (Source)** | Normal `main` integration by head `platform`; live runtime/deployment and physical cross-host adoption remain separate gates. |
| **M12b (CLI Intake/Handoff Verbs)** (`hub intake`, `hub handoff`, `python -m agent_hub_host`) | **ACCEPTED (Source)** | Included in this increment and verified with stdlib mock-HTTP and unmocked `_TokenExchange` refresh composition tests. |
| **M12b (Routing Task Assignment & Automatic Session Linkage)** (`M12-IG2`) | **OPEN (Not Implemented)** | Transactional `RoutingService` assignment (`assign`), server-side automatic propagation of `launch_id` / `native_id` / execution stages from `fleet_launches` and `fleet_sessions`, routing-owner ACK if shared routing/fleet files change, and `M8a`/`M8b`-qualified live assignment proof. |
| **M12c — Hetzner Session & Report Backfill + Assistant Adoption** (`M12-IG5`) | **OPEN (Not Implemented)** | Allowlisted/excluded bounded backfill of Hetzner `aplexer` sessions and `~/tmp/agent-reports/*.md`, seeding into the authoritative Hub, fresh `list`/`show` reconciliation receipt, and live assistant adoption. |
| **M12d — MCP Hub Exposure (`mcp.dtcdev.click`)** (`M12-IG6`) | **OPEN (Not Implemented)** | Separate MCP interface and deployment gate at `mcp.dtcdev.click`; HTTP/CLI completion does not establish MCP delivery. |
| **M12e — Managed Agent Session Orchestration; Manual Human Access = PocketShell** (`M12-IG7`) | **OPEN (Not Implemented)** | Authenticated `list` / `start` (engine/tier/host/cwd/brief) / `send` (separate durable transport vs correlated processing/ACK states) / bounded allowlisted `capture` / custody-fenced `stop`, plus end-to-end Grok Bot box $\to$ Hetzner real throwaway session loop and separate laptop/Win35 qualification (`M11`). |
| **Fresh-Agent Handoff Takeover** (`PLAN.md` `L408–412`, `L458–460`) | **Source Fixture Only** | `test/intake.test.ts:201–217` proves source-level API/CAS handoff continuation with a synthetic agent credential; live cross-host assistant takeover remains a runtime gate. |
| **Full Platform Roadmap (`M1`–`M12` + `M2b`)** | **Active** | Source acceptance of `c290454` does not integrate to `main`, deploy to Cloudflare, enroll hosts, or waive any open gate in `M3b`, `M4`, `M5`, `M7`, `M8a`, `M8b`, `M9`, `M10`, `M11`, or `M12b`–`M12e`. |

### 1.3 Mechanically Verified Git & File Lineage

| Object / Path | Commit / Blob SHA | SHA-256 Digest | Bytes | Role & Verification |
| :--- | :--- | :--- | :--- | :--- |
| **Reviewed Composed Commit (`HEAD`)** | `c290454e88c49c6fe863d20dd2717d58522851d0` | — | — | Merge of `037bf039ec5ae01799a1cc863d31178a76107cdf` (`HEAD^1`) and `adae0fb346028c449db7ce6b5858720523ffd6a9` (`HEAD^2`); immediate parent of this report |
| **Current Hub `main` (`HEAD^1`)** | `037bf039ec5ae01799a1cc863d31178a76107cdf` | — | — | Includes accepted host binding `85fdcec5e8c2fcfe0cb6d49134732b27f64e7896` / review `80921a67da3ded48ce27c772619c850cfc0e34d3` and corrected plan review `a13d227c6486221b5fb5f8b31948648cdea1f20d` |
| **Author M12a Candidate (`HEAD^2`)** | `adae0fb346028c449db7ce6b5858720523ffd6a9` | — | — | Parent `df806256fdc641fd043422edd432805368724bf4` on base `6cf0c185495a33cf04ec2b986c3c901ed3de2cda`; all 9 owned files identical in `c290454` |
| `docs/INTAKE-TASKS.md` | `be45d0129cea25d8bc7e71cf885fa454e908bdb2` | `776e603b85c6dc5abc5aefcd74c2aa79b311285a30bea4499e073d87341d3afa` | `12,336` | M12a contract & CLI documentation (195 lines) |
| `host/agent_hub_host/__main__.py` | `f67d40b90d82a8d18da172fec2adcd5d4b26a3c4` | `949c1696333eb368e473411584af54da3cb4a76d464202dd464cce904e95fe0d` | `133` | Public module entrypoint (`python -m agent_hub_host`) |
| `host/agent_hub_host/cli.py` | `9ff9f499e0e22029f5c1b0576aa041dd28d74ded` | `62b606f11375e9aee4df6e8650fe821ce3a74044194dc11c04f43e99a1b82588` | `20,838` | Registers `intake` and `handoff` subcommands; ASCII-safe JSON output & static error handling |
| `host/agent_hub_host/intake.py` | `465950a1077cec3c6385d6948b1512ccfeb501f1` | `9a31e4d50f0cb38798744e4ada3131d97eb75b76e2da7ee3eb5748e2e18d0637` | `10,535` | Stdlib client for `/api/intake/*` and bounded multi-page `handoff` assembly |
| `host/tests/test_intake.py` | `758f6ab4077c6b2e3f4877ada4d43c248fbd41f9` | `049084b0a9a82105cc683b85eee9fd37b3f3370c310eaa4ae1e53d491021e70d` | `13,531` | 15 stdlib/public CLI & mock-HTTP tests |
| `src/hub.ts` | `4b8445f4cff6fa33862417c017acb975d93f8a56` | `7f478375e2a27087caf804f0e152d573dd59da5ddf0d11c1ca014d96bfdd9361` | `139,111` | `IntakeService` wiring & `/api/intake` auth callback (`L1`, `L10`, `L77`, `L92–93`, `L322–351`) |
| `src/intake.ts` | `ab83986d0e66aa6bfff81f718a7365d9059baac1` | `749348b62a76a697e6d1af9a957cd25741bb939cba64417bf3d3ddc02e442354` | `26,231` | Durable SQLite intake ledger, grant/rule/task/receipt store & `/api/intake/*` handler (351 lines) |
| `test/intake.test.ts` | `ccc8ad5ef6311ab9e0e291a7fc37db66bdf1334a` | `a4c7d29ab0abc685402c716a9059a5321124281ee88351f5efdabd2a628b8a80` | `34,631` | 36 real Worker/SQLite HTTP tests |
| `test/vitest.intake.config.ts` | `789d75a35b5be38c2ca2c59777e7cd3f52319912` | `267290ad0e15647dd14a4a77a36a317d8d1e404c6408da49a689e4c1ae955b04` | `536` | Focused Vitest Workers pool config |

---

## 2. Deep Contract Audit Against Plan `27e4b32` & Review `a13d227`

### 2.1 Authentication, Operator Bearer, Cookie/CSRF & Least-Privilege Grants (`M12-IG1`)

- **Private Response Headers & Method Bounds (`src/intake.ts:316–325`)**: Every response from `IntakeService.handle`—including `200`, `400`, `401`, `403`, `404`, `405`, `409`, `413`, `500`, and `507`—is constructed via `json()`, which unconditionally sets `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`. Non-`GET`/`POST` methods return `405 method_not_allowed`. Cross-origin `POST` requests (`Origin` header present and not equal to `new URL(request.url).origin`) cancel the request stream and return `403 unauthorized_or_forbidden` before reading the body.
- **Mixed Cookie/Bearer Refusal & Human Session CSRF (`src/hub.ts:324, 345–349`, `src/auth/session.ts:85–96`)**: Requests carrying both `Authorization` and `Cookie` headers are immediately refused with `403 mixed_authority`. Cookie-authenticated human admin sessions require `verifyCsrf` (`x-csrf-token` matching `session.csrfToken`) on all `POST` mutations and re-verify `getSession(this.env, request)` to bind `caller.actor = 'human:' + session.sub`. Token-admin cookies (`admin-token@hub.local`) validate `data.sub === tokenAdminSubject(hash)` (`src/auth/session.ts:22–26`), so rotating `HUB_ADMIN_TOKEN_SHA256` immediately invalidates existing token-admin cookies.
- **Configured Raw Operator Bearer Token (`src/hub.ts:328–344`)**:
  - Uses the existing `HUB_ADMIN_TOKEN_SHA256` environment binding (lowercase 64-character hex via `adminTokenHash(this.env)`) on `/api/intake` routes only, without modifying `/api/bus/*`, device pairing (`validAgentPairingScopes` at `src/hub.ts:63–71`), or `/api/token/refresh`.
  - Requires `validSessionSecret(this.env)` and bounds the bearer token to `/^Bearer ([^\s]{1,4096})$/`.
  - Computes the SHA-256 `digest` once; checks `SELECT id FROM credentials WHERE token_hash=?` using `base64url(digest)` (matching `src/auth/crypto.ts:sha256` used by `credentials.token_hash`) so a revoked, expired, or non-admin registered credential can **never** fall back to operator authority even if its hash matches `HUB_ADMIN_TOKEN_SHA256`; and compares the 64-char hex digest in constant time against `configured`.
  - Rotating `HUB_ADMIN_TOKEN_SHA256` or setting an absent/malformed value fails closed (`401`). Works directly with `HUB_TOKEN` in the public CLI (`host/agent_hub_host/intake.py:27–28`).
- **Explicit Operator-Issued Intake Grants & Binding Verification (`src/intake.ts:75–82, 117–141, 258–277`)**:
  - Ordinary `agent`, `host`, `job`, and `routing-client` credentials have zero implicit intake access (`403`).
  - Only operator authority (`p.admin`) can create, list, or revoke grants (`POST/GET /api/intake/grants`, `POST /api/intake/grants/:id/revoke`).
  - `this.binding(c)` (`src/intake.ts:126–137`) verifies that the grantee is a non-revoked, non-expired `kind === 'agent'` credential with `bus:read` and without `admin` scope, resolves its canonical actor via `credentialActor(this.sql, c)` (`src/agent_registry.ts:147–159`, which checks `agents` registration and `machines.registered_at` match), and binds `{ family_id, actor, project, machine_id, registered_at, incarnation }` (where `incarnation` is `agents.rowid`).
  - Grant `expires_at` is bounded to `[now + 1, Math.min(now + 30 * 86400000, Date.parse(c.expires_at))]`.
  - Every read page, mutation, and idempotency receipt lookup re-evaluates `this.principal(auth)` synchronously inside `this.tx()`. In addition, SQLite triggers `intake_end_registration` (`AFTER DELETE ON agents`), `intake_end_binding` (`AFTER UPDATE OF tag,project,machine_id ON agents`), and `intake_end_machine` (`AFTER DELETE ON machines`) permanently set ` $.revoked_at` on affected grants and increment `intake_meta.revision`, preventing a renamed/rebound agent from reviving an old grant by moving back to its former name or machine.
  - Legitimate token refresh (`POST /api/token/refresh`) preserves `family_id`, retaining grant authorization and `family:<family_id>:<key>` idempotency receipt continuity; refresh token replay revokes the credential family (`401`); and a new credential family for the same actor (`other = await seedAgent('demo:assistant')`) has a distinct `family_id` and is denied (`403`).
  - Streamed `POST` requests call `authenticate()` both **before** reading `request.body` (`src/intake.ts:318`) and **after** `await body(request)` (`src/intake.ts:326`), followed by synchronous `this.principal(auth)` inside `this.tx()` (`src/intake.ts:328`), closing any auth-mutation window during chunk streaming.

### 2.2 Immutable Multi-Request Intake, Voice Capture, Standing Rules, Obligations & Trust Separation (`M12-IG3`, `M12-IG4`, `M12-IG6`)

- **Immutable Verbatim Intake & Voice Lifecycle (`src/intake.ts:42–48, 151–160, 208–218, 248–256`)**:
  - Task creation and `kind: 'intake'` events store verbatim `text` (preserving leading/trailing whitespace, newlines, and multibyte UTF-8 up to `8,192` UTF-8 bytes), `source: { type: 'chat' | 'voice' | 'forwarded' | 'unknown', reference, timestamp }`, server `captured_at`, authenticated `actor`, and immutable per-mutation `intake_key`. Unknown `source.timestamp` remains `null` and is never fabricated from `captured_at`.
  - Voice notes with `transcription: 'pending' | 'unavailable'` and `text: null` require `source.type === 'voice'` and non-null `source.reference` (`400 invalid_voice_capture` otherwise) and are captured immediately without invoking any external transcription service.
  - While any `kind: 'intake'` event with `text: null` lacks a subsequent `kind: 'intake'` event with `transcription: 'provided'` and matching `source.reference`, `this.continuation(task)` (`src/intake.ts:154–156`) returns `'uncertain'`, which refuses `/continue` (`409 continuation_not_ready`) and keeps the task in `/handoff` even if `status` is set to `'done'` or `'cancelled'`. Later transcript events append to `task.history` without mutating prior events.
- **Versioned Operator Standing Rules & Approval Decisions (`src/intake.ts:144–150, 229–244, 278–286`)**:
  - Only operator authority (`p.admin`) can create or amend rules (`POST /api/intake/rules`). Each rule enforces `expected_version` CAS (`0` on creation), keeps `project` scope (`null` for global or a project identifier) immutable across amendments (`400 immutable_rule_scope`), and appends `{ version, text, source, decision, actor, captured_at, authority: 'operator_rule' }` to `rule.history`.
  - Task `rule_reference` events verify the referenced rule exists, has `1 <= version <= rule.version`, and has `rule.project === null || rule.project === task.project` (`400 unknown_rule_version`).
  - Obligation resolution (`src/intake.ts:233–241`): `followup` and `blocker` events are resolved via `kind: 'resolution'` referencing an currently unresolved event ID on the same task (`400 unknown_obligation` if missing, non-obligation, or already resolved). `pending_approval` events can **only** be resolved by operator authority (`this.operator(p)`) via `kind: 'approval_decision'` with `decision: 'granted' | 'denied' | 'withdrawn'`, which records `authority: 'operator_record'`; attempting to resolve a `pending_approval` via `kind: 'resolution'` fails with `403 operator_required` even for an operator.
- **Structural Trust Separation & Safe CLI Rendering (`src/intake.ts:161–167`, `host/agent_hub_host/cli.py:264–272`)**:
  - Task and handoff responses separate `operator_rules`, `control` (`{ version, status, continuation_state, authority: 'hub_ledger_cas', execution_evidence: 'not_verified_by_intake' }`), `payload_data` (`{ title, current_state, next_step, owner, uncertainty, history }`), `unresolved`, and `data_notice`.
  - Stored original strings remain verbatim in SQLite. Public CLI output uses `json.dumps(..., indent=2, ensure_ascii=True)` (`cli.py:266`), escaping ASCII control characters (`\x1b`), C1 controls (`\x9b`), and Unicode bidi/formatting characters (`\u202e`) in terminal output while `json.loads()` recovers the exact original strings.
- **Honest Execution Provenance & Ready-to-Active CAS (`src/intake.ts:220–228, 292–296`)**:
  - `POST /api/intake/tasks/:id/continue` transitions `status` from `'open'` to `'in_progress'` via CAS only when `this.continuation(task) === 'ready'`, recording `evidence: 'recorded_active_not_started'` and refusing `active`, `uncertain`, `blocked`, `approval_blocked`, or `closed_or_waiting` tasks (`409`).
  - Manual session links record `evidence: 'external_unverified_not_launch_proof'`; report and commit links record `evidence: 'external_unverified'`; callers cannot pass an `evidence` field (`400 unknown_field`). Canonical `routing_job_id` links check `if (fields.routing_job_id && !p.admin) fail('canonical_link_requires_operator', 403)` **before** querying `routing_jobs` (so non-admin agents cannot probe foreign job existence) and still record `evidence: 'external_unverified_not_launch_proof'`.

### 2.3 CAS, Canonical Idempotency Receipts & Strict Capacity Bounds (`M12-IG3`)

- **Fresh Authorization Before Receipt Replay (`src/intake.ts:327–342`)**: Inside `this.tx()`, `this.principal(auth)` and route-level write checks (`this.operator(p)` for `/rules` and `/grants`, `this.allowed(p, t, true)` for `/tasks/:id`, `this.allowed(p, { project: payload.project, id: '' }, true)` for `/tasks`) execute **before** `this.get('receipts', id)`. A caller whose credential or write grant was revoked, expired, or downgraded to `read` cannot replay a cached write response.
- **Canonical Fingerprint & Lost-Response Replay (`src/intake.ts:37–41, 337–346`)**: Receipts are keyed by `p.identity + ':' + payload.key` (`family:<family_id>:<key>` for agents, `human-family:<family_id>:<key>` for human bearer tokens, `human:<sub_or_token_admin>:<key>` for sessions/operator token) with `fingerprint = canonical({ path, payload })`. Replaying the same key and canonical payload returns the original receipt (`200`) even after subsequent task version increments; reusing a key with a different payload or path returns `409 idempotency_conflict`.
- **Strict Streamed & Storage Capacity Limits (`src/intake.ts:9–10, 49–64, 89–101, 282–313, 343–346`)**:
  - Streamed request body reader enforces `INTAKE_LIMITS.request = 32,768` bytes on actual chunks (`413 request_capacity`), ignoring `Content-Length`, and decodes with `new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })` (`400 invalid_json` on invalid UTF-8).
  - Row/object caps: `256` tasks, `128` events per task, `128` rules, `128` history entries per rule, `256` grants, `8,192` receipts, `131,072` bytes (`128 KiB`) per serialized task, `262,144` bytes (`256 KiB`) per row in `put()`, and `4,194,304` bytes (`4 MiB`) combined `SUM(length(CAST(data AS BLOB))+length(CAST(id AS BLOB)))` across `intake_tasks`, `intake_grants`, `intake_rules`, and `intake_receipts`.
  - Any cap overrun throws `507` inside `this.tx()`, rolling back the task/rule/grant write, `intake_meta.revision` increment, and receipt insertion atomically without evicting or truncating existing records.

### 2.4 Complete Authorized Handoff & CLI Composition (`M12-IG1`, `M12-IG3`)

- **Revision- and Lease-Fenced Pagination (`src/intake.ts:168–200`)**: `/api/intake/handoff` and `/api/intake/tasks` paginate up to `25` items per page sorted by `id`. Follow-up pages (`after`) require `revision` (`400 revision_required`) and, for non-admin agents, `authorization_valid_until` (`400 authorization_snapshot_required`). Any change to `intake_meta.revision` or `p.valid_until` across pages returns `409 stale_snapshot`.
- **All-or-Nothing CLI `hub handoff` (`host/agent_hub_host/intake.py:126–171`)**: `intake.handoff()` validates `1 <= page_size <= 25`, accumulates all pages in memory while checking `snapshot_version` and `authorization_valid_until` continuity, rejects duplicate task IDs or `len(tasks) != page['total']`, restarts from scratch up to 3 times on `StaleSnapshot`, and raises `ValueError` without writing partial output to stdout if a complete consistent snapshot cannot be assembled.
- **Composition with Accepted Host Machine Binding (`85fdcec5`)**: `intake.request()` (`host/agent_hub_host/intake.py:20–49`) uses `HUB_TOKEN`/`HUB_JOB_TOKEN` when present and otherwise delegates to `auth.get_access_token()` and, on HTTP `401`, `auth.refresh_token()` (`host/agent_hub_host/auth.py:105–151`), which uses `config._TokenExchange(base_url, "/api/token/refresh", cfg)` (`host/agent_hub_host/config.py:58–131`) to validate and atomically persist refreshed tokens and `machine_id` with `0600` permissions via `private_fs.atomic_write`.

---

## 3. Preserved Failure Receipts & Independent Verification Evidence

### 3.1 Author Preserved Failure History (`/home/alexey/git/agent-hub-wt-platform-intake/.local/intake/`, Read-Only)

Inspection of the author's read-only handoff artifacts confirmed three preserved intermediate failures during development, all resolved in `adae0fb346028c449db7ce6b5858720523ffd6a9` without weakening assertions or re-pinning configuration:

1. `worker-first.json` / `worker-first.log` (`exit: 1`): Initial global-cap test attempted to insert a single 4 MiB string into one SQLite row, hitting SQLite's per-value `SQLITE_TOOBIG` limit before `IntakeService.capacity()` ran. Fixed in `test/intake.test.ts:417` by seeding 8 rows of `INTAKE_LIMITS.totalBytes / 8` bytes each and also adding a 256 KiB per-record guard in `IntakeService.put()` (`src/intake.ts:90`).
2. `worker-third.json` / `worker-third.log` (`exit: 1`): After moving `history` under `task.payload_data.history` for trust separation, one assertion at `test/intake.test.ts:165` still read `current.history`. Updated to `current.payload_data.history`.
3. `worker-freeze.json` / `worker-freeze.log` (`exit: 1`): Initial operator bearer check compared `src/auth/crypto.ts:sha256(token)` (`base64url`) against `HUB_ADMIN_TOKEN_SHA256` (64-char hex in `src/auth/session.ts:adminTokenHash`). Fixed in `src/hub.ts:332–335` by comparing the 64-char lowercase hex SHA-256 digest against `adminTokenHash(this.env)` while using `base64url(digest)` to check `credentials.token_hash`.

### 3.2 Independent Reviewer Execution Receipts (`.local/review/`)

All reviewer runs executed serially in `/home/alexey/git/agent-hub-wt-platform-intake-review` under `taskset -c 0`, `umask 077`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `PYTHONDONTWRITEBYTECODE=1`, with `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `PYTHONPYCACHEPREFIX`, and Vite `cacheDir` confined to `.local/tmp` via `.local/review/run.py` and `.local/review/vitest.intake.config.ts` (`--configLoader runner`).

| Run Label | Command | Result & Counts | Duration | Allocated Peak (`.local`) | Apparent Peak (`.local`) | Max Process Group RSS | Cgroup Memory Peak / Pids Peak |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `typecheck-exact` | `node_modules/.bin/tsc --noEmit` | **PASS** (`exit: 0`) | `7.90s` | `495,616` B | `482,885` B | `240,631,808` B | `429,916,160` B / `18` |
| `host-exact` | `python3 -m unittest host.tests.test_intake -v` | **PASS** (`15/15`, `exit: 0`) | `19.21s` | `507,904` B | `486,273` B | `70,438,912` B | `429,916,160` B / `18` |
| `worker-exact` | `node_modules/.bin/vitest run --config .local/review/vitest.intake.config.ts --configLoader runner` | **PASS** (`36/36`, `exit: 0`) | `37.11s` | `11,976,704` B | `11,922,775` B | `620,285,952` B | `715,075,584` B / `40` |
| `host-adversarial` | `python3 .local/review/test_intake_adversarial.py -v` | **PASS** (`4/4`, `exit: 0`) | `2.25s` | `589,824` B | `543,911` B | `37,171,200` B | `715,075,584` B / `40` |
| `worker-adversarial` | `REVIEW_TEST_INCLUDE=.local/review/intake-adversarial.probe.test.ts ...` | **Preserved Probe Failure** (`2/9`, `exit: 1`: probe `beforeEach` called `createSessionCookie` before resetting `env.SESSION_SECRET`) | `9.40s` | `1,269,760` B | `1,204,940` B | `649,904,128` B | `737,189,888` B / `43` |
| `worker-adversarial-r2` | `REVIEW_TEST_INCLUDE=.local/review/intake-adversarial.probe.test.ts ...` | **Preserved Probe Failure** (`9/10`, `exit: 1`: probe passed `' premature'` with leading space as `key`, hitting `400 invalid_identifier` before `409`) | `17.92s` | `2,924,544` B | `2,845,759` B | `657,502,208` B | `737,189,888` B / `43` |
| `worker-adversarial-r3` | `REVIEW_TEST_INCLUDE=.local/review/intake-adversarial.probe.test.ts ...` | **PASS** (`10/10`, `exit: 0`) | `14.68s` | `3,014,656` B | `2,928,232` B | `667,308,032` B | `743,215,104` B / `43` |

### 3.3 Summary of 14 Novel Reviewer Adversarial Probes

1. **Worker/SQLite HTTP (`intake-adversarial.probe.test.ts`, 10/10 PASS)**:
   - `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff` on `200`/`401`/`404`/`405` (`PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`) and `/api/intake` root path.
   - Configured operator bearer token (`HUB_ADMIN_TOKEN_SHA256`) rejected (`401`/`403`) on non-intake routes (`/api/bus/inbox`, `/api/fleet`, `/api/agents`), rejected on `> 4096`-byte or whitespace-suffixed tokens (`401`), and disabled (`401`) when `SESSION_SECRET` is invalid (`< 32` chars).
   - Valid `host`, `job`, `routing-client`, `bus:read`-less `agent`, and `admin`-scoped `agent` credentials rejected (`403`) on `/api/intake/handoff` and rejected (`400 invalid_grantee`) on `POST /api/intake/grants`.
   - Grant expiry bounds (`<= now`, `> 30d`, `> cred.expires_at` all `400`) and write-to-read grant downgrade preventing cached write-receipt replay (`403` on `/tasks`, `404` on `/tasks/:id`).
   - Receipt namespace isolation across distinct agent credential families (`family-one` vs `family-two` using identical `key`) and `409 idempotency_conflict` on same-family cross-path key reuse.
   - `transcription: 'unavailable'` and multi-media (`media:1`, `media:2`) null-text voice notes keeping `continuation_state === 'uncertain'` and refusing `/continue` (`409`) until both media references have `transcription: 'provided'` intake events, plus `400 invalid_identifier` on leading-space `key`.
   - Obligation resolution rejecting `note` targets (`400`), operator `resolution` on `pending_approval` (`403 operator_required`), `approval_decision` on `followup` (`400`), and duplicate resolution (`400`), plus `status: 'cancelled'` with non-null `uncertainty` remaining visible in `/handoff`.
   - Rejection (`400`) of boolean/string/float/negative `expected_version` and unknown fields, and `413 request_capacity` on a `> 32 KiB` chunked stream carrying a spoofed `Content-Length: 10` header.
   - Atomic `507` rollback on rule `history` exhaustion (`128` entries) and single-record byte exhaustion (`> 256 KiB` in `put()`).
   - Mid-stream credential revocation after `await body(request)` caught by the second `authenticate()` check (`401` with zero SQLite mutation), and prompt-injection payload (`{"operator_rules":...}` + ANSI/C1/bidi controls) isolated in `payload_data` without affecting `operator_rules` or `control`.
2. **Python Public CLI & Host-Binding Composition (`test_intake_adversarial.py`, 4/4 PASS)**:
   - Unmocked `cli.main()` (`hub intake create`) $\to$ `intake.request()` $\to$ HTTP `401` $\to$ `auth.refresh_token()` $\to$ `config._TokenExchange` (`POST /api/token/refresh`) atomically updating `access_token`, `refresh_token`, and `machine_id: 'rebound-machine-01'` in `0600` `host.json` and succeeding on retry.
   - Malformed refresh receipt (`machine_id: 'bad\nmachine'`) rejected by `_TokenExchange.record()`, preserving prior `0600` `host.json` untouched and exiting `1` without echoing tokens on `stderr`.
   - `intake.handoff()` restarting when `authorization_valid_until` changes across pages at the same `snapshot_version`, and rejecting invalid `--page-size` (`0`, `26`, `-1`).
   - `hub intake update` rejecting simultaneous `--uncertainty` and `--clear uncertainty` (`exit: 1`) before making any network request.

### 3.4 Containment, Scratch & Peer Preservation Receipt

- **Containment Limits (`a whoami --json`)**: `memory.max = 1,572,864,000` B (`1500M`), observed `memory.peak = 743,215,104` B (`47.25%`); `pids.max = 256`, observed `pids.peak = 43` (`16.80%`).
- **Scratch Peaks (`.local` combined `.local/review` + `.local/tmp`, against `16 MiB` early guard / `20 MiB` hard cap)**:
  - Active sampled peak (during `worker-exact` with live Miniflare/Vite cache): `11,976,704` B allocated (`71.39%` of `16 MiB` early guard; `57.11%` of `20 MiB` hard cap) / `11,922,775` B apparent (`48` peak inodes).
  - Post-run retained `.local` footprint: `634,880` B allocated / `524,124` B apparent.
- **Scope & Peer Hygiene**: Zero tracked source, test, config, or prior-report files modified; only `docs/reviews/INTAKE-TASKS.md` committed on direct parent `c290454e88c49c6fe863d20dd2717d58522851d0`. All peer worktrees and sessions preserved untouched.
