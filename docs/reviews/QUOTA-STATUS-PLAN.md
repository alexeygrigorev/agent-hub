# Independent Argon Plan Review: M13 Quota Dashboard & Routing Admission and M14 One-Call Status (`6750e34bc2fdff07c84e0127c973ac46210f9e5d`)

- **Reviewer**: `platform-quota-status-plan-review` (`gemini-4-argon-high`, native session `1f2004e9-3d3f-45f1-926e-1875058539e8`)
- **Agent Branches Task / Branch**: `0432` / `platform-qstatus-0432`
- **Review Worktree**: `/home/alexey/git/agent-hub-wt-platform-quota-status-plan-review`
- **Hub Base Commit**: `6bc91a1278250b374ea1d0b11acd353fae51a8cb` (`docs: add Argon conversation post-landing review [skip ci]`)
- **Coordinator Integration Owner**: `platform` (`codex`, session `e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363`) in `/home/alexey/git/agent-coordination`
- **Authoritative Reviewed Coordinator Pin**: `6750e34bc2fdff07c84e0127c973ac46210f9e5d` (`docs: checkpoint hub status scope and active repair reviews [skip ci]`)
- **Reviewed Coordinator Artifacts**:
  - `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` (git blob `e058f3ca6264cb64b2d1d581e70c8fc04ff4c37b`, `50,239` bytes, `727` lines, SHA-256 `aeeeab5803cda380764901aa3b1689a57fb629ced57b0b33314becf2a669885c`)
  - `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/STATUS.md` (git blob `e40b8fbc41fb17a7c530e915fcecb4106fd74049`, `106,065` bytes, `1,662` lines, SHA-256 `0c6fa184f4cb8db08b1b9e5fcfc305af35c38d3876f16a6c2a42ca44a3ecd38c`)
- **Authoritative Operator Briefs Read First**:
  1. `/home/alexey/tmp/agent-reports/hub-platform-brief.md` (`2,403` bytes, SHA-256 `b13681ab123336d42399ef4c924fdc9d3009f56420ae43a437f4925886f4e0f7`)
  2. `/home/alexey/tmp/agent-reports/hub-intake-brief.md` (`3,805` bytes, SHA-256 `af48f48242bad7235a3a8a652b67e1694e816c1a935ba4f1657b903ae1d2b7f1`, Base Brief + Addenda 1–4)
- **Prior Preserved Argon M12 Plan Review**: `docs/reviews/INTAKE-TASKS-PLAN.md` (commit `27e4b32248109bf03a408444ca3e87ce1a4db984`, blob `ccb96d07b8dfa99ade9af09880e8a230ae64c7a3`, SHA-256 `9223d3e93d8e5e89e4ac37eac1f9ec71e0d5934e35f0f75e7dcea08adc1ec7e4`)
- **Review Date**: `2026-10-17`
- **Verdict**: **SCOPED ACCEPT** (plan-only on exact coordinator pin `6750e34bc2fdff07c84e0127c973ac46210f9e5d`, subject to binding implementation and acceptance gates `M13-IG1`..`M13-IG3` and `M14-IG1`..`M14-IG4` and strict `M12e`-first ordering; **zero implementation, source, or runtime acceptance is granted by this review**)

Review: approved sha=6750e34bc2fdff07c84e0127c973ac46210f9e5d reviewer=platform-quota-status-plan-review/gemini-4-argon-high

---

## 1. Executive Summary and Scoped Verdict

Pinned coordinator commit `6750e34bc2fdff07c84e0127c973ac46210f9e5d` extends `docs/platform/PLAN.md` and `docs/platform/STATUS.md` with two new milestones derived from Alexey's full intake brief (`/home/alexey/tmp/agent-reports/hub-intake-brief.md`, Addenda 3 and 4) while preserving the platform consolidation baseline (`/home/alexey/tmp/agent-reports/hub-platform-brief.md`) and earlier accepted `M12` plan contracts (`docs/reviews/INTAKE-TASKS-PLAN.md`):

1. **`M13 — Quota dashboard and routing admission` (`PLAN.md:549–604`)**, split into `M13a` (host collection and authenticated quota snapshots), `M13b` (Hub API, `hub quota` CLI, and `dakit` dashboard view), and `M13c` (quota-aware routing and launch admission).
2. **`M14 — Status in one call` (`PLAN.md:606–685`)**, split into `M14a` (canonical status contract, API, and `hub status` CLI), `M14b` (every-host per-task session health and task-linked report headline/status provenance), `M14c` (landed-`main` vs currently active effective deployed revision including rollbacks and failed/pending deploys, independent CI state, and pending Alexey actions/approvals/device-login codes), and `M14d` (`--since` window and bounded caller-scoped diff since the caller's last status check).

### Why the Verdict is `SCOPED ACCEPT` (and Not `REVISE` on the Plan Pin)

Every substantive requirement in `hub-platform-brief.md` and `hub-intake-brief.md` (Base + Addenda 1–4)—including the critical deployment-inclusion and rollback refinements landed in coordinator commit `7d8d0f7d93fa7624c232b871b4d6766766786032` and checkpointed in `6750e34bc2fdff07c84e0127c973ac46210f9e5d`—is explicitly represented in `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and `STATUS.md`:

- **Primary objective and ordering preserved**: `M12e` (native `aplexer` session control, launch/create, attach/watch, and end-to-end `Grok Bot` -> Hub -> Hetzner live worker handoff and completion loop) remains **Priority 1** ahead of `M13` and `M14` implementation (`PLAN.md:41–44, 90, 426, 600, 611, 685, 698–705`; `STATUS.md:36–61, 1626–1662`).
- **All seven prior `M12` Argon contract requirements preserved**: `PLAN.md:466–540` retains `M12-C1` through `M12-C7` from `docs/reviews/INTAKE-TASKS-PLAN.md` (`27e4b32248109bf03a408444ca3e87ce1a4db984`) verbatim.
- **No parallel tracker, controller, bus, or quota/status store**: `M13` and `M14` extend the existing Cloudflare Worker + single SQLite Durable Object (`src/hub.ts`), existing quota/fleet/routing/intake modules (`src/quota.ts`, `src/fleet.ts`, `src/routing.ts`, `src/model-routing.ts`, `src/intake.ts`), and existing Python stdlib host agent/CLI (`host/agent_hub_host/`).
- **Critical operational invariants are already in the pinned plan text**:
  - `M13` explicitly requires provider-native units (`percent_remaining`, reset timestamps, Codex credits, OpenCode Go spend), forbids fabricating dollar/token values from percentages, forbids uploading raw OAuth tokens/cookies/keys/prompt history, fails closed on stale/missing/unknown quota during routing and launch claim, preserves the 15% Codex and 5% Grok reserves, and states that a passed `reset_at` timestamp never implies capacity recovery without a fresh post-reset observation (`PLAN.md:555–604`).
  - `M14` explicitly requires every-host per-task session health (`working`, `waiting`, `stopped`, `limit-hit`, `stuck`, `uncertain`), read-only detection of `Weekly/Monthly Limit Exhausted`, low-context warnings, and typed-but-unsubmitted composer drafts (**never** sending `Enter`, keystrokes, or auto-restarts from status collection), bounded task-linked report headline/verdict provenance distinguishing author claims from independent review verdicts, landed `main` vs **currently active deployed revision** (handling rollbacks and newer failed/pending deploys without false-positive deployed flags), CI reported separately from deploy, pending Alexey approvals/decisions/device-login codes (with `user_code` restricted to the authorized owner and tokens never shown), and bounded caller-scoped status diffs (`PLAN.md:606–685`).

### Why Approval is Scoped to Seven Concrete Source-Level Implementation Gates (`M13-IG1`..`M13-IG3`, `M14-IG1`..`M14-IG4`)

Comparing `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` against the actual Hub codebase at `6bc91a1278250b374ea1d0b11acd353fae51a8cb` surfaced seven concrete code-level mismatches between current `main` implementations and the planned `M13`/`M14` contracts. None of these require rewriting `PLAN.md` before implementation starts, **provided** the implementers and reviewers of `M13a–c` and `M14a–d` treat findings **`F-M13-1`..`F-M13-3`** and **`F-M14-1`..`F-M14-4`** (Section 5 and Section 6 below) as mandatory acceptance gates:

1. **`F-M13-1` / Gate `M13-IG1` (Medium — Quota DTO & Split Table Contract)**: `src/quota.ts` (`QUOTA_SCHEMA_VERSION = "1.0"`, `L36–59, L365–379, L440–445`) currently rejects any window field except `percent_remaining` (`400 unknown_field`), and `src/fleet.ts:26–38, 87–91` strips `fleet_subscriptions.quota_json` to `percent_remaining` while ignoring `host_quota_snapshots` whenever `route.profile !== null` or multiple accounts exist on a host. `M13a/b` must version/extend the sanitized schema and bridge `host_quota_snapshots` + `fleet_subscriptions` to carry Codex credits, OpenCode Go spend in native units, `collector_version`, and profile/account qualification (`zcodex` vs `codex`, `muse` profile).
2. **`F-M13-2` / Gate `M13-IG2` (Medium — Single-Host `GET /api/quota` & Missing `hub quota` CLI)**: `src/quota.ts:924–1000` currently restricts `GET /api/quota` to a host reading its own `machine_id` or a browser admin session supplying a mandatory single `?host_id=<id>`, rejecting `agent`, `routing-client`, and admin bearer tokens (`403`), and `host/agent_hub_host/cli.py` has no `hub quota` command. `M13b` must add `hub quota` and an authenticated fleet-wide/filterable quota view with shared-account (`fleet_accounts.label`) deduplication.
3. **`F-M13-3` / Gate `M13-IG3` (High — Routing Admission Bypass on Stale Subscriptions, Partial Window Errors & Passed `reset_at`)**: `src/routing.ts:106–107` and `src/model-routing.ts:22–26` currently do **not** check `subscription.observed_at` or `quota.observed_at` freshness, ignore `quota.status !== 'ok'`, `limit_reached === true`, and `stale === true`, ignore sibling windows with `error: 'unavailable'` (`present: false, percent_remaining: null`) when at least one window has a finite number, do not reconcile passed `reset_at` timestamps against `observed_at`, and do not re-verify quota at `POST /api/fleet/launches/:id/claim` (`src/fleet.ts:225–240`). `M13c` must close all four gaps fail-closed (with routing-owner coordination).
4. **`F-M14-1` / Gate `M14-IG1` (Medium — `hub status` CLI Collision & Single-Call Scoped Authority)**: `host/agent_hub_host/cli.py:44–54, 177, 343–344` already defines `hub status` as a local-config stub (`Status: Logged in. Base URL: ...`). `M14a` must upgrade `hub status` to call `GET /api/status` (preserving local config diagnostics via flag or header field) and enforce synchronous transaction-time caller scoping and explicit partial-coverage reporting when any host is offline/stale.
5. **`F-M14-2` / Gate `M14-IG2` (High — Read-Only Session Observations & Sanitized Report Provenance)**: `PUT /api/fleet/sessions` (`src/fleet.ts:284–309`, `host/agent_hub_host/fleet.py:234–244`) currently reports only basic `aplexer list` fields. `M14b` must add strictly read-only session health observations (`limit_hit_kind`, `context_low`, `has_unsubmitted_draft`, `draft_byte_length`, `detection_source`—never sending `Enter` or uploading draft/transcript text) and allowlisted report headline/verdict metadata distinguishing `author_claim` vs `independent_review_verdict`.
6. **`F-M14-3` / Gate `M14-IG3` (High — Effective Active Deployment Ancestry, Rollbacks & Independent CI)**: The Hub currently has no Git/CI/deployment ledger. `M14c` must compute `deployed` strictly from whether a landed `main` commit is an ancestor of the **currently active deployed revision** for the target environment so rollbacks (`wrangler rollback`) demote rolled-back commits to `not_deployed (rolled_back)` and newer failed/pending deploy runs never overwrite the last confirmed active deployment, while CI state remains independent.
7. **`F-M14-4` / Gate `M14-IG4` (Medium — Pending Human Action Privacy & Bounded Caller-Scoped Diff Watermarks)**: Joining `device_pairings` (`src/schema.ts:27–44`) into `M14c` must never expose `user_code` to non-admin agent tokens (and never expose `device_code_hash` or tokens to anyone), and `M14d` stateful diff cursors must be keyed by authenticated `(caller_principal_id, scope_fingerprint)` with hard row caps and explicit `expired` / `first_observation` baseline reporting.

---

## 2. Exact Verified Lineage, Digests, and Non-Acceptance Boundaries

### 2.1 Coordinator Commit Lineage Leading to Reviewed Pin `6750e34bc2fdff07c84e0127c973ac46210f9e5d`

All coordinator evidence was inspected read-only via `git -C /home/alexey/git/agent-coordination show 6750e34bc2fdff07c84e0127c973ac46210f9e5d:...` (never from mutable working copies):

| Coordinator Commit SHA | Short | Subject | Role in Reviewed Pin |
| :--- | :--- | :--- | :--- |
| `6750e34bc2fdff07c84e0127c973ac46210f9e5d` | `6750e34` | `docs: checkpoint hub status scope and active repair reviews [skip ci]` | **Authoritative reviewed pin** (`PLAN.md` + `STATUS.md`) |
| `7d8d0f7d93fa7624c232b871b4d6766766786032` | `7d8d0f7` | `docs: require current deployment inclusion and rollback status [skip ci]` | Added active-revision inclusion, rollback demotion, and failed/pending deploy preservation to `M14c` |
| `58885564b5f24dd8652a3ffa4f0f1550a9f4f7e3` | `5888556` | `docs: record session repair and independent metrics R4 review [skip ci]` | Recorded `M12e` R2 repair candidate `2ca4700` and `M9` R4 review findings |
| `af22b1c11c04c5167972b0ba83fa2ca97337c27a` | `af22b1c` | `docs: add one-call hub status milestone from Alexey addendum 4 [skip ci]` | Introduced `M14` (`M14a–M14d`) from `hub-intake-brief.md` Addendum 4 |
| `45d8f6d121bf2838ff699974ec8988d91bddf212` | `45d8f6d` | `docs: record session review defects and runtime activation evidence [skip ci]` | Added `M13` (`M13a–M13c`) from Addendum 3 and recorded `M12e` R1 Argon `REVISE` |
| `7555734769b65629b8bd8697918b07798a73c2bf` | `7555734` | `docs: preserve hub priorities and metrics review repair ownership [skip ci]` | Preserved `M12e` priority and `M9` metrics repair ownership |

### 2.2 Verbatim Operator Briefs and Pinned Coordinator Artifacts

| Artifact / Brief | Exact Source / Git Ref | Bytes | Lines | Git Blob SHA | SHA-256 Digest |
| :--- | :--- | ---: | ---: | :--- | :--- |
| `hub-platform-brief.md` | `/home/alexey/tmp/agent-reports/hub-platform-brief.md` | `2,403` | `10` | *(filesystem)* | `b13681ab123336d42399ef4c924fdc9d3009f56420ae43a437f4925886f4e0f7` |
| `hub-intake-brief.md` | `/home/alexey/tmp/agent-reports/hub-intake-brief.md` | `3,805` | `13` | *(filesystem)* | `af48f48242bad7235a3a8a652b67e1694e816c1a935ba4f1657b903ae1d2b7f1` |
| `docs/platform/PLAN.md` | `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` | `50,239` | `727` | `e058f3ca6264cb64b2d1d581e70c8fc04ff4c37b` | `aeeeab5803cda380764901aa3b1689a57fb629ced57b0b33314becf2a669885c` |
| `docs/platform/STATUS.md` | `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/STATUS.md` | `106,065` | `1,662` | `e40b8fbc41fb17a7c530e915fcecb4106fd74049` | `0c6fa184f4cb8db08b1b9e5fcfc305af35c38d3876f16a6c2a42ca44a3ecd38c` |

### 2.3 Inspected Hub Base (`6bc91a1278250b374ea1d0b11acd353fae51a8cb`) Files

| Hub File Path (`6bc91a1278250b374ea1d0b11acd353fae51a8cb`) | Bytes | Lines | Git Blob SHA | SHA-256 Digest |
| :--- | ---: | ---: | :--- | :--- |
| `AGENTS.md` | `4,147` | `45` | `898fdc0eccf663cf7ed8908503bf7a43527d0bda` | `75debbd4568b55239982736755d461b5c1376592481110d23d15af0b6b9685e2` |
| `docs/SPEC.md` | `37,304` | `288` | `f0e11e806283d325421990048c35fb78c57ced0b` | `3922a202c092dfc22948ac210c9de2d265b4b76798440371e0527123da4efc84` |
| `docs/QUOTA.md` | `14,770` | `163` | `a43d76fbb0f918439a42ca7d590296261478e038` | `13dc05b856d20ffaf2510a2b32404b89e9db8410b94b60b755eb238010883963` |
| `docs/ROUTING.md` | `13,466` | `141` | `27de3235bf33c2314f3811929c65282ef8909415` | `43b89762dca66318f98c4b40c2ea2230d1981f00ed65f991edb1624a63887acb` |
| `docs/INTAKE-TASKS.md` | `12,336` | `195` | `be45d0129cea25d8bc7e71cf885fa454e908bdb2` | `776e603b85c6dc5abc5aefcd74c2aa79b311285a30bea4499e073d87341d3afa` |
| `docs/DEPLOY.md` | `13,134` | `205` | `30d432b8578ab4869f77e99b6904ce42604b748c` | `630a3bd5c1cb46487059b79b22837fc49dd76eac253c22c6bfaa05ea9c4e6a90` |
| `docs/reviews/INTAKE-TASKS-PLAN.md` | `66,449` | `272` | `ccb96d07b8dfa99ade9af09880e8a230ae64c7a3` | `9223d3e93d8e5e89e4ac37eac1f9ec71e0d5934e35f0f75e7dcea08adc1ec7e4` |
| `config/model-routing.json` | `3,286` | `167` | `adbac5d7983e1a6fdf4695ee6c6bae7a9b5268ec` | `94da9df16a807160d9e608857388f711fb13f36187db227b53360dbde957b242` |
| `src/quota.ts` | `30,579` | `1,013` | `6242c4597ac222a0a5edb65867f53fffd3c0db37` | `b4745dc9e463730cce3c7efd58ca2a69b4aa59b939000f8867cd3e0e8638f11e` |
| `src/routing.ts` | `25,302` | `247` | `6432bcf34d19ac254b0bc0da1ae0142d5fbc49f0` | `7614418591b63b2f673ef2cf0ef08f4bce09405898e79d40965a1bb6335e5847` |
| `src/model-routing.ts` | `2,818` | `30` | `3cf67075f6074f412f5bf32dea22912af10b09f3` | `52b3e79bdd10bcef8e69a1fe7b805bfaa6a6b4893bbf1351d3a77ddbf9bdc252` |
| `src/fleet.ts` | `34,804` | `362` | `7ed969d1feef36c10ee1c82524e06d5bfdf631be` | `95f12961ff4633439f35e39e441d89443fa325c562d0ed2e9939944bc68a32a9` |
| `src/intake.ts` | `26,231` | `351` | `ab83986d0e66aa6bfff81f718a7365d9059baac1` | `749348b62a76a697e6d1af9a957cd25741bb939cba64417bf3d3ddc02e442354` |
| `src/hub.ts` | `142,703` | `2,696` | `02e8946946a9f9c6a8f5514e4b0863be816e825c` | `50f67c0ec73a5816bbe094b4e6c394aa4326feeb7500144ae64535560743829c` |
| `src/schema.ts` | `4,884` | `217` | `9e4405b15229bbfb898624d18ba03f212337c39f` | `1dacf018852752ad82e4342276be036b9cbf8c7387eccbbf5281e47a13619ded` |
| `src/checks.ts` | `19,995` | `269` | `2d5ebcbccfda69c5ac338edf4c4b4776bcf0a4b4` | `1f348199d7f6d42b8d0742df6533fbc860a23f73b33ec07f3b4e7ad8448fc8f8` |
| `src/liveness.ts` | `13,492` | `200` | `05551fffd6ae01bd229c39277fb9d4a23408f5e9` | `ca2ff4548e066814c1e0be4bdfeebda4a2a8e8b92d42b8e06c1e771da9ec752f` |
| `host/agent_hub_host/quota_snapshot.py` | `27,475` | `776` | `163251a9c0076d86136a585a07507181d0bb73fe` | `fb3d7c4a66d89de74499a7d703eefb57e3e503cfa82fb3361527e11160a43461` |
| `host/agent_hub_host/fleet.py` | `24,518` | `406` | `b9fd76117de59f509ebb6f0094b29cd93eaed3b3` | `3dc3e8489a66dc153b4852275792def30efcdc82060343989b01ae0804cd5947` |
| `host/agent_hub_host/cli.py` | `20,838` | `413` | `9ff9f499e0e22029f5c1b0576aa041dd28d74ded` | `62b606f11375e9aee4df6e8650fe821ce3a74044194dc11c04f43e99a1b82588` |
| `host/agent_hub_host/intake.py` | `10,535` | `214` | `465950a1077cec3c6385d6948b1512ccfeb501f1` | `9a31e4d50f0cb38798744e4ada3131d97eb75b76e2da7ee3eb5748e2e18d0637` |
| `host/agent_hub_host/oversight.py` | `84,790` | `1,943` | `e4efd127e57b3c92bad9df390bd3c6575c62eb5a` | `6fbfd6f12cf148d7d1b3285872bb8c2dc54bd42ae6724397e3b30ad49a018ab1` |
| `host/agent_hub_host/oversight_adapter.py` | `98,535` | `2,209` | `026062d1f10737b58ad112a7a035c6ebfa8d4d5d` | `3f0d01d4acab9523de31d64a8c50abdafdaf3b3e1b9761b2be7cee50d919b5a1` |
| `host/agent_hub_host/task_authority.py` | `93,240` | `2,104` | `939cc3f0534eeca5b6717ced5bd371377631970a` | `df07955388e67bfeca08d764d75d4e76a8b2e89210ed09d3ee94ee0da4fe64bf` |
| `public/fleet.html` | `10,093` | `86` | `8741a8cf925264712fb7cc8c504ca61d4b9be2f1` | `be30ad8043ba51d70f4cf6d151eeebb9221b0bdeff50682f722286fe091530fe` |
| `public/fleet.js` | `12,113` | `119` | `3bd6065186e3dbc9cac7992eae08231dab51f6d9` | `902f8d9632830ee655c79c41c1c0e3d9452b51f50bb769fc02a28147aa4c85e6` |

### 2.4 Strict Source vs Runtime Non-Acceptance Ledger

To prevent any conflation of plan approval with implementation or runtime readiness, the exact acceptance state as of `6750e34bc2fdff07c84e0127c973ac46210f9e5d` and Hub `main` `6bc91a1278250b374ea1d0b11acd353fae51a8cb` is:

| Slice / Capability | Hub `main` (`6bc91a1`) Source State | Candidate / Branch State | Live Cross-Host Runtime State |
| :--- | :--- | :--- | :--- |
| **`M10` Role leases & `M11` Conversation retention** | Source-only accepted on `main` (`6bc91a1`) | Landed on `main` | **Not runtime-accepted** (no live ops custody ACK) |
| **`M12a` Intake & task links** | Source-only accepted on `main` (`477bd23` / `6bc91a1`) | Landed on `main` | **Not runtime-accepted** (no live Botbox -> Hetzner AI loop proven) |
| **`M12e` Native `aplexer` session control** | **Not on `main`** | R2 candidate `2ca4700dd92a1731ce10a0ed2a1c423997149492` under separate Argon review (`platform-session-r2-review`) | **Unaccepted**; local unit/fixture/shell/native protocol tests are **not** cross-host AI acceptance |
| **`M9` Metrics & supervision telemetry** | **Not accepted** (R1–R4 rejected with `REVISE`) | R5 candidate `3504795d54d40bc818db9671283d6a22db2a0f25` under active refinement (`platform-metrics-r5`) | **Unaccepted** |
| **`M13` Quota dashboard & routing admission** | Existing `M3`/`M7` baseline only (`src/quota.ts`, `src/fleet.ts`, `src/routing.ts`, `src/model-routing.ts`) | **Planned in `6750e34`** (`M13a–M13c`) | **Not implemented / not accepted** |
| **`M14` One-call `hub status`** | Stub `hub status` local-config print only (`host/agent_hub_host/cli.py:44–54`) | **Planned in `6750e34`** (`M14a–M14d`) | **Not implemented / not accepted** |

---

## 3. Requirement-by-Requirement Audit Against `hub-platform-brief.md` and `hub-intake-brief.md` (All Four Addenda)

### 3.1 Platform Consolidation Brief (`/home/alexey/tmp/agent-reports/hub-platform-brief.md`)

| Brief Requirement | Pinned Plan Location (`6750e34:docs/platform/PLAN.md`) | Hub `6bc91a1` Source Baseline | Audit Verdict |
| :--- | :--- | :--- | :--- |
| Hub is the single durable control plane for agent registry, liveness, conversations, task/launch records, quota snapshots, and supervision; `aplexer` is the per-machine execution plane | `L13–39`, `L81–94`, `L409–457`, `L549–685` | `src/hub.ts`, `src/fleet.ts`, `src/quota.ts`, `src/intake.ts` | **SATISFIED IN PLAN** |
| Retire standalone daemons (`desktop-orchestrator`, `agent-dashboard`, `principal-ping`, `supervision`, bus `:8788`) only after replacement slices pass migration gates | `L32–39`, `L96–154`, `L698–727` | Preserved in `docs/SPEC.md` §12 and `PLAN.md` | **SATISFIED IN PLAN** |
| Cloudflare Workers Free plan budget (`<= USD 5/month`), bounded SQLite Durable Object storage, no unauthorized deploys | `L19–25`, `L532–536`, `L649–650`, `L681` | Enforced in `src/schema.ts`, `src/quota.ts`, `src/intake.ts` | **SATISFIED IN PLAN** |

### 3.2 Intake, Session Control, Quota, and One-Call Status Brief (`/home/alexey/tmp/agent-reports/hub-intake-brief.md`)

| Brief Section & Verbatim Requirement | Pinned Plan Location (`6750e34:docs/platform/PLAN.md`) | Hub `6bc91a1` Source Observation & Gap | Audit Verdict |
| :--- | :--- | :--- | :--- |
| **Base Brief (`L1–5`)**: Conversational assistant (`Grok Bot` on Botbox, future assistants) creates/tracks Hub tasks, dispatches work to `Hetzner` / `ovh2` / `mac` / `win` / `botbox`, receives heartbeats/blockers/completions, and surfaces follow-ups without polling panes | `M12` (`L409–547`), `M12e` (`L426, L459–465`), Priority order (`L41–44, L698–705`) | `M12a` landed (`src/intake.ts`, `host/agent_hub_host/intake.py`); `M12e` R2 (`2ca4700`) under separate review; live cross-host AI loop still unproven | **SATISFIED IN PLAN** (`M12e` kept Priority 1) |
| **Addendum 1 (`L7`)**: Task intake & orchestration must live in `agent-hub`, not in `Grok Bot` or `aplexer`; `aplexer` provides host-local session primitives (`list`, `watch`, `send`, `capture`, `create`/`launch`); Hub routes across hosts | `L411–445`, `L466–506` (`M12-C1`..`M12-C4`) | `src/intake.ts` + `src/fleet.ts` own task/launch state; `aplexer` remains host-local | **SATISFIED IN PLAN** |
| **Addendum 2 (`L9`)**: Keep Muse Spark 1.3 (`opencode-go/muse-spark-1.3-contributor`) + Z.ai GLM-5.1 (`zcodex`) as primary coding engines; preserve Codex 15% and Grok 5% reserves; prefer Z.ai Flash (`glm-4.7-flash` / `glm-4.5-flash`) for lightweight tasks when qualified | `L581–582`, `L597–598` | `config/model-routing.json:15–165` encodes `muse` + `zcodex` default pools and `codex` 15% / `grok` 5% reserves | **SATISFIED IN PLAN** (subject to `M13-IG3`) |
| **Addendum 3 (`L11`) — `M13a`**: Collect & normalize usage/quota across every enrolled host (`Hetzner`, `ovh2`, `mac`, `win`, `botbox`) for **Codex** (weekly remaining %, credits), **Z.ai** (5h/weekly windows, resets), **Claude**, **Grok**, **Antigravity/Gemini**, and **OpenCode Go** (spend + Muse Spark profile); preserve provider-native units; never fabricate dollar/token counts from %; never upload raw OAuth tokens/cookies/keys | `M13a` (`L555–568`, `L584–589`, `L601`) | `host/agent_hub_host/quota_snapshot.py` & `src/quota.ts` only accept `percent_remaining` in schema `"1.0"` and lack Codex credits, OpenCode Go spend, `collector_version`, and profile keys in `host_quota_snapshots` | **SATISFIED IN PLAN** — requires **`M13-IG1`** |
| **Addendum 3 (`L11`) — `M13b`**: Expose quota in Hub API, `hub quota` CLI, and `dakit` UI; show per-host and shared-account pools without double-counting; distinguish `fresh`, `stale`, `exhausted`, `unavailable`, `unknown`; protect private usage/account inventory with Hub auth | `M13b` (`L570–575`, `L590–593`, `L602`) | `GET /api/quota` (`src/quota.ts:924–1000`) is single-host only (`?host_id=`), rejects `agent`/`routing-client`/admin bearer tokens, and `cli.py` has no `hub quota` subcommand | **SATISFIED IN PLAN** — requires **`M13-IG2`** |
| **Addendum 3 (`L11`) — `M13c`**: Routing & launch admission must consume normalized quota snapshots, exclude exhausted engines/accounts/windows, fail closed on stale/missing/unknown quota, re-check at launch claim, and never treat a passed `reset_at` timestamp alone as recovered capacity | `M13c` (`L577–582`, `L594–598`, `L603`) | `src/model-routing.ts:22–26` & `src/routing.ts:106–107` skip subscription freshness checks, ignore sibling `unavailable` windows, ignore `reset_at` vs `observed_at`, and `src/fleet.ts:225–240` does not re-check quota on claim | **SATISFIED IN PLAN** — requires **`M13-IG3`** |
| **Addendum 4 (`L13`) — `M14a`**: One-call `hub status` (`hub status`, `hub status --since 2h`, `hub status --json`) + authenticated API joining intake tasks, sessions on every host, reports, Git/CI/deploy state, and pending human actions; partial coverage when a host is offline/stale; per-section source/time/freshness | `M14a` (`L613–625`, `L632–640`, `L651–654`, `L672–675`) | `host/agent_hub_host/cli.py:44–54, 343–344` currently binds `hub status` to a local-config print stub; no `/api/status` route exists yet | **SATISFIED IN PLAN** — requires **`M14-IG1`** |
| **Addendum 4 (`L13`) — `M14b`**: Every-host per-task session status (`native_id`, host, engine/profile, `working`/`waiting`/`stopped`/`limit-hit`/`stuck`/`uncertain`); detect `Weekly/Monthly Limit Exhausted`, low-context warnings, and typed-but-unsubmitted composer drafts; **status collection is strictly read-only and never submits `Enter` or restarts jobs**; task-linked report headline/status line with author-claim vs independent-review provenance; no raw prompt history/drafts/secrets uploaded | `M14b` (`L627–628`, `L642–645`, `L655–659`, `L676–677`) | `PUT /api/fleet/sessions` (`src/fleet.ts:284–309`) only stores basic `aplexer list` fields; no session limit/context/draft or report-headline metadata exists yet | **SATISFIED IN PLAN** — requires **`M14-IG2`** |
| **Addendum 4 (`L13`) + Coordinator `7d8d0f7` — `M14c`**: Landed `main` commits vs deployed commits where `deployed` requires a successful deploy run whose **currently active deployed revision** includes the commit; rollbacks mark rolled-back commits as no longer deployed; newer failed/pending deploys do not replace the last confirmed active deployment; CI reported separately; pending Alexey approvals/decisions/device-login codes (`user_code` only to authorized owner, never tokens) | `M14c` (`L629`, `L646–648`, `L660–667`, `L678–680`) | No Git/CI/deployment ledger exists in `src/schema.ts` yet; `device_pairings` (`src/schema.ts:27–44`) stores `user_code` and `device_code_hash` | **SATISFIED IN PLAN** — requires **`M14-IG3` & `M14-IG4`** |
| **Addendum 4 (`L13`) — `M14d`**: `--since <window>` (e.g. `2h`) and short caller-scoped diff since the caller's last status check; bounded auth-scoped cursors/snapshots; expired/unavailable baseline reported honestly without inventing diffs | `M14d` (`L630`, `L649–650`, `L668–670`, `L681–683`) | No caller-scoped status watermark table exists in `src/schema.ts` yet | **SATISFIED IN PLAN** — requires **`M14-IG4`** |

---

## 4. Priority Discipline, Architecture, and Milestone Slicing Review

### 4.1 Preservation of `M12e` Priority 1 and Active Repair Discipline

A key risk when adding `M13` and `M14` to the platform plan is scope preemption—diverting engineering or review capacity away from `M12e` (live cross-host `aplexer` session control and conversational handoff) and `M9` (supervision metrics R5 repair). Pinned `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` explicitly prevents this in five places:

1. **Top-level sequencing note (`PLAN.md:41–44`)**: Places `M12e` (`session-control`) and `M9` (`metrics` R5 repair) ahead of `M13` and `M14` implementation.
2. **Milestone map (`PLAN.md:81–94`)**: Orders `M12` before `M13` and `M14` and marks `M12e` as the active blocker for end-to-end conversational orchestration.
3. **`M13` and `M14` milestone dependencies (`PLAN.md:600, 611, 685`)**: Explicitly state that `M13` and `M14` do not preempt `M12e` acceptance.
4. **Immediate Next Actions (`PLAN.md:698–705`)**:
   - Priority 1: Complete `M12e` R2 independent Argon review (`2ca4700dd92a1731ce10a0ed2a1c423997149492`) and prove real `Grok Bot` -> Hub -> Hetzner live worker loop.
   - Priority 2: Complete `M9` R5 repair (`3504795d54d40bc818db9671283d6a22db2a0f25`) and independent Argon re-review.
   - Priority 3: Only after `M12e` plan/gate alignment, execute `M13a -> M13b -> M13c` and `M14a -> M14b -> M14c -> M14d` in small reviewable slices.

### 4.2 Additive Sub-Milestone Cuts Without Circular Dependencies

Both new milestones are decomposed into small, acyclic, independently testable vertical slices:

- **`M13a -> M13b -> M13c`**:
  - `M13a` normalizes host collection and Hub snapshot ingestion (`host/agent_hub_host/quota_snapshot.py`, `host/agent_hub_host/fleet.py`, `src/quota.ts`, `src/fleet.ts`).
  - `M13b` builds read projections (`GET /api/quota`, `hub quota` CLI in `host/agent_hub_host/cli.py`, and `public/fleet.html` + `public/fleet.js` dakit views) on top of `M13a`.
  - `M13c` wires `RoutingService.dispatch()`, `modelCandidates()`, and `FleetService.claim()` to consume the normalized quota state from `M13a/b` without creating a second quota store.
- **`M14a -> M14b -> M14c -> M14d`**:
  - `M14a` establishes the `GET /api/status` projection contract and `hub status` CLI over already-landed Hub tables (`intake_*`, `routing_jobs`, `fleet_launches`, `fleet_sessions`, `agents`, `alerts`, `check_runs`) plus `M13` quota summaries.
  - `M14b` enriches `M14a` with host-collected read-only session health signals (`limit-hit`, low-context, unsubmitted draft) and sanitized task-linked report headline/verdict provenance.
  - `M14c` adds bounded Git `main` vs active deployed revision (with rollback & failed/pending deploy tracking), independent CI status, and pending Alexey actions/approvals/device-login codes.
  - `M14d` adds `--since <window>` filtering and bounded caller-scoped diff watermarks over the canonical `M14a–c` status snapshot.

---

## 5. Deep Technical Review of `M13` (`QUOTA`): Source Reality vs Pinned Plan

### Finding `F-M13-1` (Medium — Binding Gate `M13-IG1`): Quota DTO Schema Version `1.0` & Split `host_quota_snapshots` vs `fleet_subscriptions` Storage Drop Native Units and Profile Routes

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **Strict schema `1.0` field rejection in `src/quota.ts`**:
   - `src/quota.ts:36–59` defines `QUOTA_SCHEMA_VERSION = "1.0"`, `ALLOWED_TOP_KEYS`, `ALLOWED_PROVIDER_KEYS = ["status", "observed_at", "windows", "details"]`, and `ALLOWED_WINDOW_KEYS = ["percent_remaining", "reset_at", "error"]`.
   - `src/quota.ts:365–379` (`validateWindow`) rejects any window object containing keys outside `ALLOWED_WINDOW_KEYS` with `400 unknown_field`, and requires `percent_remaining` to be `number | null` in `[0, 100]`.
   - `src/quota.ts:440–445` (`validateDetails`) restricts `details` to `limit_reached`, `stale`, and (for `grok` only) `has_grok_code_access`.
   - Consequently, `src/quota.ts` **cannot currently ingest** Codex credit balances, OpenCode Go spend in native units, `collector_version`, or per-profile metadata (`muse` vs other OpenCode Go profiles, `zcodex` vs `codex`).
2. **Host collector gaps in `host/agent_hub_host/quota_snapshot.py`**:
   - `quota_snapshot.py:134–425` collects `claude`, `gemini`, `codex`, `grok`, `zai`, and `go` (`opencode`), normalizing every provider window solely to `percent_remaining` (`0..100`) and `reset_at`. It does not emit `collector_version`, Codex credit balances, or OpenCode Go native spend fields.
3. **Split storage and profile/multi-account exclusion in `src/fleet.ts`**:
   - `host_quota_snapshots` (`src/quota.ts:24–33`) is keyed solely by `host_id TEXT PRIMARY KEY` (one JSON blob per machine, indexed by provider name `claude | gemini | codex | grok | zai | go`).
   - `FleetService.observation()` (`src/fleet.ts:87–91`) explicitly refuses to read `host_quota_snapshots` whenever `route.profile !== null` or a machine has more than one distinct account for that provider (`if (route.profile !== null) return null;` and `if (accounts.size !== 1) return null;`), falling back exclusively to `fleet_subscriptions` (`PUT /api/fleet/subscriptions`, `src/fleet.ts:310–328`).
   - Meanwhile, `sanitizedQuota()` in `src/fleet.ts:26–38` strips `fleet_subscriptions.quota_json` down to `{status, observed_at, windows: {<k>: {present, percent_remaining, reset_at, error}}, details: {limit_reached, stale}}`, discarding any native credit/spend/collector metadata.

#### Required Implementation & Acceptance Check (`M13-IG1`)
- **Contract adjustment for `M13a`**:
  1. Extend the sanitized quota contract (e.g. additive schema `"1.1"` backward-compatible with `"1.0"` readers, or explicitly allowlisted optional fields in `src/quota.ts` and `sanitizedQuota()` in `src/fleet.ts`) to allow bounded, secret-free provider-native usage fields:
     - `collector_version` (bounded ASCII string, e.g. `<= 64` chars) at the snapshot/subscription envelope.
     - Optional window/provider native-unit fields: `credits_remaining: number | null`, `spend_used: number | null`, `spend_limit: number | null`, `unit: 'percent' | 'credits' | 'usd_cents' | 'requests' | 'tokens' | null`, and `profile: string | null` (e.g. `opencode-go/muse-spark-1.3-contributor`, `zcodex`).
  2. Maintain strict fail-closed unknown-key rejection (`400 unknown_field`) and secret-key rejection (`access_token`, `refresh_token`, `id_token`, `api_key`, `cookie`, `authorization`, `session_token`, `email`, `account_id`, `organization_id`, `raw_output`).
  3. Bridge `host_quota_snapshots` and `fleet_subscriptions` so profile-qualified routes (`zcodex`, `muse`) and multi-account hosts (`codex-1`, `codex-2`) have a single consistent source of truth for `M13b` display and `M13c` routing, without fabricating dollar or token amounts from percentage-only windows.
- **Executable verification gate**: Unit and fixture tests in `test/quota.test.ts` and `host/tests/test_quota_snapshot.py` must verify that: (a) all six provider families (`codex`, `zai`, `claude`, `grok`, `gemini`, `go`) round-trip native units + `collector_version` through both host collection and Hub ingestion, (b) any raw token/cookie/email/unknown key is rejected with `400`, and (c) percentage-only windows never synthesize dollar or token numbers.

---

### Finding `F-M13-2` (Medium — Binding Gate `M13-IG2`): `GET /api/quota` Single-Host / Cookie-Only Read Restriction, Missing `hub quota` CLI, and `dakit` UI Coverage

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **`GET /api/quota` (`src/quota.ts:924–1000`) cannot serve fleet-wide or assistant CLI reads**:
   - `src/quota.ts:930–938` rejects `agent`, `routing-client`, and admin bearer (`HUB_TOKEN`) callers with `403 Forbidden`.
   - For browser admin sessions (`auth.role === "admin"`), `src/quota.ts:951–963` requires a single `?host_id=<machine_id>` query parameter (`if (!requestedHost) return jsonError(400, "explicit_host_id_required", ...)`). There is no multi-host summary, no provider/engine filter, and no join against `fleet_accounts` / `fleet_subscriptions`.
2. **No `hub quota` subcommand in `host/agent_hub_host/cli.py`**:
   - `host/agent_hub_host/cli.py:318–389` registers `login`, `status`, `agents`, `inbox`, `send`, `ack`, `register`, `heartbeat`, `oversight`, and `intake`. There is no `quota` subcommand.
3. **`public/fleet.js:28–38` dakit UI limitations**:
   - `public/fleet.js:34` formats subscription windows as ``${w.percent_remaining ?? 'unknown'}% left / ${w.percent_remaining === null ? 'unknown' : 100 - w.percent_remaining}% used; reset ${w.reset_at || 'unknown'}`` and does not surface Codex credits, OpenCode Go native spend, `collector_version`, or explicit per-window `fresh | stale | exhausted | unavailable | unknown` badges.

#### Required Implementation & Acceptance Check (`M13-IG2`)
- **Contract adjustment for `M13b`**:
  1. Extend `GET /api/quota` (or `GET /api/quota` with optional `?host_id=`, `?provider=`, `?engine=`, `?account=`) to authenticate:
     - Human admin sessions (`hub_session`) and admin bearer tokens for full fleet-wide and per-host/per-account views.
     - Host bearer tokens (`role === "host"`) for their bound `machine_id` (preserving existing host isolation).
     - Authorized orchestrator/assistant tokens (verified against current active credential/family and registry incarnation per `M12-C1`) for authorized quota summaries.
     - Unauthenticated or revoked/stale-incarnation callers must be rejected with `401`/`403`.
  2. Deduplicate shared accounts (`fleet_accounts.label`, e.g. `codex-1`, `zai-shared`) across hosts (`Hetzner`, `ovh2`, `mac`, `win`, `botbox`) so a single shared subscription reported by two machines is displayed as one shared capacity pool with per-host observation provenance, never summed as double capacity.
  3. Add `hub quota [--host <id>] [--provider <p>] [--engine <e>] [--account <label>] [--json]` in `host/agent_hub_host/cli.py`.
  4. Update the `dakit` dashboard (`public/fleet.html` / `public/fleet.js` or dedicated quota section) to render native units (Codex weekly % + credits, Z.ai 5h/weekly + resets, OpenCode Go spend + Muse Spark profile, Claude, Grok, Antigravity/Gemini) and explicit `fresh | stale | exhausted | unavailable | unknown` state badges using `textContent` / DOM nodes only (preserving the `public/fleet.js:1` XSS invariant).

---

### Finding `F-M13-3` (High — Binding Gate `M13-IG3`): Routing Quota Admission Bypasses Stale Subscriptions, Partial Window Errors (`unavailable`), Passed `reset_at` Timestamps, and Launch-Claim Re-Checks

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
Inspecting `src/routing.ts`, `src/model-routing.ts`, and `src/fleet.ts` revealed four concrete fail-open defects in how routing currently consumes quota:

1. **`RoutingService.dispatch()` (`src/routing.ts:106–107`) never checks `subscription.observed_at` or `quota.observed_at` freshness**:
   - In `src/routing.ts:95–108`, `dispatch()` checks `fresh(machine.last_checkin_at, at)` (`<= 180,000 ms`), meaning the **host heartbeat** must be within 3 minutes, but it parses `subscription.quota_json` without checking `freshQuota(subscription.observed_at, at)` (`<= 1,200,000 ms` / 20 minutes) or `quota.observed_at`! If a host is online (`last_checkin_at` fresh) but its quota collector stopped updating 6 hours ago, `RoutingService.dispatch()` treats the 6-hour-old `quota_json` as current!
2. **`modelCandidates()` (`src/model-routing.ts:22–26`) ignores `quota.status`, `limit_reached`, `stale`, and sibling `unavailable` windows**:
   - Compare `FleetService.accountStatus()` (`src/fleet.ts:104–116`) with `modelCandidates()` (`src/model-routing.ts:22–26`):
     - `FleetService.accountStatus()` checks `q.status !== "ok" || q.details?.limit_reached !== false || q.details?.stale === true` and `windows.some((w) => w.error === "unavailable")`.
     - `modelCandidates()` in `src/model-routing.ts:22–26` does **none of those checks**! It writes:
       ```ts
       const windows = Object.values(observation.quota?.windows ?? {}).filter(
         (window) => window.present !== false && Number.isFinite(window.percent_remaining),
       );
       const remaining = windows.length ? Math.min(...windows.map((window) => Number(window.percent_remaining))) : null;
       ```
     - Because `src/quota.ts:819–830` (`projectWindowForRead`) sets `present: false, percent_remaining: null, error: "unavailable"` when a window fails to parse or is unavailable, `modelCandidates()` **filters out** the broken/unavailable window! If a provider has two windows (`five_hour` with `error: "unavailable", present: false` and `seven_day` with `percent_remaining: 80`), `modelCandidates()` computes `remaining = 80` and admits the route! Even worse, if `quota.details.limit_reached === true` or `quota.details.stale === true` while `percent_remaining` is still above reserve, `modelCandidates()` still admits the route!
3. **Passed `reset_at` timestamps are not reconciled against `observed_at`**:
   - Neither `modelCandidates()` (`src/model-routing.ts`) nor `FleetService.accountStatus()` (`src/fleet.ts:104–116`) compares `Date.parse(w.reset_at)` against `Date.parse(quota.observed_at)` and `now`. If a window was observed as exhausted (`percent_remaining: 0` or `limit_reached: true`) at `10:00Z` with `reset_at: 10:05Z`, and at `10:06Z` no new post-reset snapshot (`observed_at >= 10:05Z`) has arrived yet, any logic that treats `reset_at <= now` as resetting the window would falsely admit an unverified route; conversely, if a window's `reset_at` has passed without a fresh snapshot, the snapshot is stale relative to its own window boundary.
4. **`FleetService.claim()` (`src/fleet.ts:225–240`) does not re-verify quota at launch claim time**:
   - When a host claims an `offered` launch via `POST /api/fleet/launches/:id/claim`, `FleetService.claim()` checks machine drain state and `activeCount >= account.max_concurrency` (`src/fleet.ts:233–236`), but does **not** re-check `this.accountStatus(launch.account, launch.service, at)` or route reserve gates. If an account became exhausted or stale between `POST /api/fleet/launches` (or `POST /api/routing/jobs`) and host claim, the launch still proceeds.

#### Required Implementation & Acceptance Check (`M13-IG3`)
- **Contract adjustment for `M13c` (with routing-owner coordination)**:
  1. Extract a single canonical helper `evaluateQuotaAdmission(observation, reservePercent, nowMs)` shared by `FleetService.accountStatus()`, `modelCandidates()`, `RoutingService.dispatch()`, and `FleetService.claim()`:
     - Reject (`status: 'unknown' | 'stale'`) if `!observation` or `ageMs(observation.observed_at, nowMs) > QUOTA_STALE_AFTER_MS` (`1,200,000 ms`) or `ageMs(observation.quota?.observed_at, nowMs) > QUOTA_STALE_AFTER_MS`.
     - Reject (`status: 'exhausted' | 'stale' | 'unknown'`) if `quota.status !== 'ok'`, `quota.details?.stale === true`, or `quota.details?.limit_reached !== false` (and for `grok`, require `quota.details?.has_grok_code_access === true`).
     - Reject (`status: 'unknown'`) if **any** window in `quota.windows` has `w.error === 'unavailable'` or `(w.present === true && !Number.isFinite(w.percent_remaining))`.
     - Reject (`status: 'stale'`) if any present window has a finite `reset_at` where `Date.parse(w.reset_at) <= nowMs` **and** `Date.parse(quota.observed_at) < Date.parse(w.reset_at)` (i.e., the reset boundary has passed, so the pre-reset snapshot is stale until a fresh post-reset observation `observed_at >= reset_at` arrives).
     - Compute `remaining = Math.min(...presentWindows.map(w => w.percent_remaining))` over all present windows (`presentWindows.length >= 1` required) and reject (`status: 'exhausted'`) if `remaining <= Math.max(0, reservePercent)` (preserving Codex `15%` and Grok `5%` reserves from `config/model-routing.json`).
  2. Re-run `evaluateQuotaAdmission` inside `FleetService.claim()` (`POST /api/fleet/launches/:id/claim`) so a route whose quota became exhausted, stale, or unavailable while `offered` fails closed at claim admission rather than starting an exhausted session.
  3. Ensure engine/model preference order (Muse Spark 1.3 `opencode-go/muse-spark-1.3-contributor` and Z.ai `zcodex` / Z.ai Flash `glm-4.7-flash` / `glm-4.5-flash` for lightweight tasks) is applied **only** among routes that pass `evaluateQuotaAdmission` and qualification gates.

---

## 6. Deep Technical Review of `M14` (`STATUS`): Source Reality vs Pinned Plan

### Finding `F-M14-1` (Medium — Binding Gate `M14-IG1`): `hub status` CLI Stub Collision & Single-Call Scoped Authority Projection

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **`hub status` already exists in `host/agent_hub_host/cli.py:44–54, 177, 343–344` as a local-only config check**:
   - `cmd_status` (`cli.py:44–54`) reads `~/.config/agent-hub/config.json`, prints `"Status: Logged in. Base URL: <url>"` (or `"Status: Not logged in."`), and exits without making any HTTP request to the Hub.
2. **Caller authority scoping across multi-table projections**:
   - `src/intake.ts:75–123` (`resolveIntakeActor`) enforces that an `agent` bearer token can access intake tasks only when backed by an active, non-expired `intake_grants` row matching the caller's current `credential_id` / `credential_family_id` and `registry_incarnation` (`M12-C1`).
   - A single-call `GET /api/status` joins intake tasks, sessions across all hosts, task-linked reports, quota summaries, Git/CI/deploy state, and pending human actions. If `GET /api/status` were restricted only to browser admin cookies, `Grok Bot` or CLI automation could not call `hub status --json`; conversely, if `GET /api/status` returned unscoped fleet state to any agent token, it would violate `M12a` task/project scoping.

#### Required Implementation & Acceptance Check (`M14-IG1`)
- **Contract adjustment for `M14a`**:
  1. Upgrade `cmd_status` in `host/agent_hub_host/cli.py` to invoke `GET /api/status` with support for `--since <duration|iso>` (e.g. `--since 2h`), `--task <task_id>`, `--project <project>`, `--host <machine_id>`, and `--json` (retaining local config/auth-file status in a `client_auth` metadata block or `--local-only` flag so existing local troubleshooting still works).
  2. In `GET /api/status`, verify caller authority inside the synchronous Durable Object transaction:
     - Human admin session (`hub_session`) or admin bearer token (`HUB_TOKEN`) receives the full fleet projection.
     - Authorized intake/orchestrator caller (with active `intake_grants` matching current `credential_id`/`credential_family_id` and current `registry_incarnation`) receives tasks, linked sessions, and linked reports scoped to its granted `projects` (or `['*']`), plus authorized fleet/quota/deploy summaries.
     - Unauthenticated, expired, revoked, or ungranted callers fail closed (`401`/`403`).
  3. Every top-level section of the `GET /api/status` response (`tasks`, `sessions`, `reports`, `quota`, `commits_and_deploys`, `pending_human_actions`, `diff`) must carry explicit provenance metadata: `{source, observed_at, freshness: 'fresh' | 'stale' | 'unavailable' | 'unknown', coverage: 'complete' | 'partial' | 'unavailable', missing_hosts: string[]}`. When one or more enrolled hosts (`Hetzner`, `ovh2`, `mac`, `win`, `botbox`) are offline or stale, `GET /api/status` must return `coverage: 'partial'` with the remaining hosts' data rather than failing the whole status call or pretending offline hosts are idle.

---

### Finding `F-M14-2` (High — Binding Gate `M14-IG2`): Read-Only Every-Host Session Health (Limit-Hit, Low-Context, Unsubmitted Composer Draft) and Task-Linked Report Provenance

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **Current session reporting (`src/fleet.ts:284–309`, `host/agent_hub_host/fleet.py:234–244`) lacks terminal health signals**:
   - `collect_sessions()` in `host/agent_hub_host/fleet.py:234–244` runs `aplexer list --json` and maps each session to `{native_id, tag, workspace, engine, profile, account, phase, agent_state, last_activity_at}`.
   - `PUT /api/fleet/sessions` (`src/fleet.ts:284–309`) stores only those columns in `fleet_sessions`.
   - `LivenessService` (`src/liveness.ts:52–135`) classifies a session only as `working`, `idle`, `stalled` (when `phase === 'working'` and `last_activity_at` is older than threshold), or `unknown` (when machine heartbeat is stale).
   - Neither `fleet.py` nor `src/fleet.ts` inspects read-only `aplexer` pane/session state for `Weekly/Monthly Limit Exhausted` banners, low-context warnings, or typed-but-unsubmitted composer drafts, nor does it link sessions back to `intake_tasks.task_id` when a session was attached or spawned outside `fleet_launches`.
2. **No task-linked report headline/verdict collection exists yet**:
   - Reports written to `/home/alexey/tmp/agent-reports/*.md` or `<workspace>/docs/reviews/*.md` are not indexed or summarized in the Hub.

#### Required Implementation & Acceptance Check (`M14-IG2`)
- **Contract adjustment for `M14b`**:
  1. **Strictly read-only session observation**:
     - Host session collection (`host/agent_hub_host/fleet.py` / `aplexer` read-only status/capture inspection) must **never** call `aplexer send`, never inject `Enter` or keystrokes, never clear or submit a composer draft, and never auto-restart or retry a stopped/limit-hit session.
     - Extend `fleet_sessions` (or a joined `fleet_session_observations` table) with bounded, sanitized columns:
       - `task_id: string | null` (resolved from `intake_task_links` / `fleet_launches` / session metadata)
       - `health_state: 'working' | 'waiting' | 'stopped' | 'limit-hit' | 'stuck' | 'uncertain'`
       - `limit_hit_kind: 'weekly_limit_exhausted' | 'monthly_limit_exhausted' | 'rate_limit' | 'quota_gate' | null`
       - `context_low: boolean | null` and `context_remaining_percent: number | null`
       - `has_unsubmitted_draft: boolean | null` and `draft_byte_length: number | null` (**never** upload the raw draft text, prompt history, or terminal scrollback to the Hub)
       - `detection_source: string` (e.g. `'aplexer_metadata' | 'pane_banner_scan'`) and `observed_at: string`
  2. **Sanitized task-linked report headline & provenance contract**:
     - Host report collection may inspect **only** explicitly linked report paths under allowlisted roots (`~/tmp/agent-reports/*.md` and `<workspace>/docs/reviews/*.md`, rejecting `..`, symlinks escaping allowlisted roots, or dotfile/credential paths).
     - Upload only bounded metadata (`<= 512` bytes per report): `report_path`, `task_id`, `sha256`, `mtime`, sanitized single-line `headline` (`<= 240` chars, control chars stripped), parsed `verdict_token` (`APPROVED | SCOPED_ACCEPT | REVISE | IN_PROGRESS | UNKNOWN`), and explicit `verification_kind: 'author_claim' | 'independent_review_verdict' | 'coordinator_integrated'`.
     - `hub status` must never label an implementer's self-report (`verification_kind === 'author_claim'`) as an accepted review verdict (`independent_review_verdict` requires a matching `Review: approved sha=... reviewer=...` trailer or recorded independent review link).

---

### Finding `F-M14-3` (High — Binding Gate `M14-IG3`): Landed `main` Commits vs Currently Active Effective Deployment (Rollbacks & Failed/Pending Deploys) and Independent CI State

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
- On `6bc91a1278250b374ea1d0b11acd353fae51a8cb`, `src/schema.ts` has no tables for Git commits on `main`, CI check runs, or active environment deployments (`docs/DEPLOY.md` is a static operator runbook).
- Coordinator commit `7d8d0f7d93fa7624c232b871b4d6766766786032` (checkpointed in `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` at `L629, L646, L660–664, L678–679`) specifically tightened `M14c` so that `deployed` is **not** a naive timestamp comparison (`deploy_run.completed_at >= commit.committed_at`).

#### Required Implementation & Acceptance Check (`M14-IG3`)
- **Contract adjustment for `M14c`**:
  1. **Active Deployment State Machine**:
     - Model per-environment deployment state (`environment`, e.g. `production`) with two distinct records:
       - `active_deployment`: `{environment, active_deployment_id, active_revision_sha, activated_at, deployed_by, is_rollback, previous_revision_sha, ancestry_commits_json}` representing the **currently live revision** serving traffic.
       - `latest_deploy_attempt`: `{attempt_id, target_revision_sha, status: 'succeeded' | 'failed' | 'pending' | 'cancelled' | 'rolled_back', started_at, finished_at, detail}` representing the most recent deploy workflow or operator action.
  2. **Four mandatory deployment truth rules**:
     - **Rule 1 (Ancestry inclusion)**: A landed `main` commit `C` has `deployed: true` if and only if there is a confirmed `active_deployment` for the target environment and `C` is identical to or a verified Git ancestor of `active_deployment.active_revision_sha`.
     - **Rule 2 (Rollback demotion)**: When a rollback (`wrangler rollback` or re-deploy of an older revision `R_old`) succeeds, `active_deployment.active_revision_sha` updates to `R_old` with `is_rollback: true`. Any landed `main` commit `C` that was in the rolled-back revision `R_new` but is **not** an ancestor of `R_old` must immediately report `deployed: false`, `deploy_state: 'rolled_back'`, and reference the active revision `R_old`.
     - **Rule 3 (Failed or pending newer deploy preservation)**: When a deploy attempt for a newer revision `R_next` is `pending` or `failed`, `active_deployment` **must remain unchanged** at the last confirmed live revision (`R_live`). Commits in `R_live` remain `deployed: true`, while commits in `R_next \ R_live` report `deployed: false`, `deploy_state: 'deploy_failed' | 'deploy_pending'`, and `active_revision_sha: R_live`.
     - **Rule 4 (CI independence)**: Each landed `main` commit records `ci_state: 'pass' | 'fail' | 'running' | 'skipped' | 'unknown'` (with workflow run ID/timestamp) as a completely separate field from `deploy_state`. Passing CI never implies `deployed: true`, and `deployed: true` never implies `ci_state: 'pass'`.

---

### Finding `F-M14-4` (Medium — Binding Gate `M14-IG4`): Pending Alexey Actions (Approvals, Blockers, Device Login Codes) & Bounded Caller-Scoped Diff Watermarks

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **Device pairing secrets in `src/schema.ts:27–44` and `src/hub.ts:520–680`**:
   - `device_pairings` stores `id`, `device_code_hash`, `user_code`, `label`, `role`, `machine_id`, `tag`, `created_at`, `expires_at`, `approved_at`, `approved_by`, `denied_at`, `consumed_at`, `issued_token_id`.
   - While `device_code_hash` is a SHA-256 digest of the device polling secret, `user_code` is the short human approval code entered at `/device` by an authenticated admin (`src/hub.ts:588–640`). If `GET /api/status` exposed raw `user_code` values to non-admin agent tokens, an untrusted or compromised agent token could observe pending `user_code` values.
2. **Stateful "since last `hub status` call" diff storage**:
   - `M14d` requires both explicit `--since <window>` (e.g. `2h`) and a short diff since the caller's last status check (`new completions`, `newly stuck or limit-hit sessions`, `newly landed/deployed commits`, `newly resolved or newly opened blockers`). Without per-caller scoping and strict row bounds, status watermarks could either clobber across concurrent callers (e.g. Alexey's CLI vs `Grok Bot`) or grow unbounded in DO SQLite.

#### Required Implementation & Acceptance Check (`M14-IG4`)
- **Contract adjustment for `M14c` & `M14d`**:
  1. **Pending Alexey actions & device-code privacy (`M14c`)**:
     - Aggregate pending human actions from three existing/planned sources:
       - Unresolved `intake_tasks` where `human_State` / obligation is `pending_approval`, `blocker`, or `followup` (`src/intake.ts:147–150`).
       - `routing_jobs` / `fleet_launches` in state `waiting-on-human`.
       - Pending, non-expired, unapproved/undenied `device_pairings` (`approved_at IS NULL AND denied_at IS NULL AND consumed_at IS NULL AND expires_at > now`).
     - **Strict privacy gate**: `device_pairings.user_code` (along with `label`, `role`, `machine_id`, `expires_at`) may be included **only** when the `GET /api/status` caller is an authenticated human admin (`auth.role === 'admin'`, or the initiating owner session). Non-admin agent/orchestrator callers must never receive `user_code`, and `device_code_hash`, access tokens, refresh tokens, or session cookies must **never** appear in any status response.
  2. **Bounded caller-scoped diff watermarks (`M14d`)**:
     - Key status diff watermarks by `(caller_key, scope_key)` where `caller_key` is derived from the authenticated principal (`admin:<email>` or `cred:<credential_family_id>`) and `scope_key` is the normalized filter tuple (`project`, `task_id`, `host_id`).
     - Store a compact digest/watermark summary (`observed_at`, `expires_at`, bounded JSON snapshot of task states, session health states, active deployment SHA, and open blocker IDs, `<= 16 KiB` per watermark row, capped at `<= 256` total rows with LRU/TTL eviction).
     - Support a read-only/no-advance query parameter (e.g. `?advance_cursor=0`) if a caller wants to preview status without advancing its watermark, and when a caller's prior watermark has expired or was evicted, return `diff: {baseline_status: 'expired' | 'first_observation', since_at: ...}` rather than fabricating a false zero-change diff.

---

## 7. Summary of Binding Implementation & Runtime Acceptance Gates

| Gate ID | Target Slice | Severity | Mandatory Contract & Acceptance Requirement |
| :--- | :--- | :--- | :--- |
| **`M13-IG1`** | `M13a` | Medium | Extend sanitized quota schema (`src/quota.ts`, `src/fleet.ts`, `host/agent_hub_host/quota_snapshot.py`) with `collector_version`, Codex credits, OpenCode Go native spend, and profile/account qualification (`zcodex`, `muse`) while keeping unknown/secret key rejection fail-closed (`400`) and never fabricating dollar/token counts from percentages. |
| **`M13-IG2`** | `M13b` | Medium | Add `hub quota` CLI (`host/agent_hub_host/cli.py`), extend `GET /api/quota` for authenticated fleet-wide and filtered reads (admin + current-incarnation authorized orchestrator tokens), deduplicate shared `fleet_accounts.label` pools across hosts, and render `fresh \| stale \| exhausted \| unavailable \| unknown` badges and native units in `public/fleet.html` + `public/fleet.js`. |
| **`M13-IG3`** | `M13c` | **High** | Enforce fail-closed quota admission in `RoutingService.dispatch()`, `modelCandidates()`, and `FleetService.claim()` (with routing-owner ACK): require fresh `subscription.observed_at` and `quota.observed_at` (`<= 1200 s`), `quota.status === 'ok'`, `limit_reached === false`, `stale === false`, zero `unavailable` windows, all present windows `> reserve_percent` (Codex 15%, Grok 5%), and `observed_at >= reset_at` before a passed `reset_at` clears an exhausted window. |
| **`M14-IG1`** | `M14a` | Medium | Upgrade `hub status` CLI (`host/agent_hub_host/cli.py`) from local-config stub to single-call `GET /api/status` (`--since`, `--json`), enforce synchronous transaction-time caller scoping (`M12-C1`), and report per-section `{source, observed_at, freshness, coverage, missing_hosts}` with graceful `partial` coverage when hosts are offline/stale. |
| **`M14-IG2`** | `M14b` | **High** | Collect every-host per-task session health (`working \| waiting \| stopped \| limit-hit \| stuck \| uncertain`, `limit_hit_kind`, `context_low`, `has_unsubmitted_draft`, `draft_byte_length`) strictly read-only (**never** sending `Enter`/keys, restarting jobs, or uploading raw draft/transcript text), and collect allowlisted task-linked report headlines/verdicts with explicit `author_claim` vs `independent_review_verdict` provenance. |
| **`M14-IG3`** | `M14c` | **High** | Compute `deployed` for landed `main` commits strictly from Git ancestry in the **currently active deployed revision** (`active_revision_sha`): rollbacks (`wrangler rollback`) must demote rolled-back commits to `not_deployed (rolled_back)`, newer failed/pending deploys must not replace the last confirmed active deployment, and CI state (`ci_state`) must remain separate from `deploy_state`. |
| **`M14-IG4`** | `M14c` / `M14d` | Medium | Join pending human actions (`intake` approvals/blockers/follow-ups, `waiting-on-human` jobs, pending `device_pairings`) with `user_code` restricted strictly to authenticated human admins (never exposing `device_code_hash` or tokens), and key `M14d` diff watermarks by `(caller_key, scope_key)` with hard row/byte caps and honest `expired \| first_observation` baseline reporting. |

---

## 8. Rollout, Fallback, and Cross-Host Runtime Acceptance Order

1. **Gate 0 — Finish `M12e` and `M9` R5 First**:
   - Complete the independent Argon R2 review of `M12e` (`2ca4700dd92a1731ce10a0ed2a1c423997149492`) and prove the live cross-host loop (`Grok Bot` on Botbox -> Hub intake/task -> Hetzner native `aplexer` session -> progress/completion return), alongside `M9` R5 (`3504795d54d40bc818db9671283d6a22db2a0f25`) acceptance.
2. **Gate 1 — `M13a -> M13b -> M13c` Rollout & Fallback**:
   - Deploy/land Hub schema `1.1` additive quota ingestion (`M13a`) and `GET /api/quota` + `hub quota` (`M13b`) before enabling older/newer host collectors across `Hetzner`, `ovh2`, `mac`, `win`, and `botbox`.
   - During rollout, hosts still sending schema `1.0` snapshots remain readable for `percent_remaining` windows; providers or hosts lacking new native-unit collectors report `unknown` / `unavailable` for uncollected fields rather than failing the host check-in.
   - When `M13c` is enabled, any route with `stale`, `unavailable`, `unknown`, or `exhausted` quota fails closed for automatic routing while leaving manual human override (`waiting-on-human` / explicit admin inspection via `hub quota`) intact.
3. **Gate 2 — `M14a -> M14b -> M14c -> M14d` Rollout & Fallback**:
   - `M14a` (`GET /api/status` + `hub status`) works immediately over existing Hub tables and reports `coverage: 'partial'` for hosts that have not yet upgraded their host agent to emit `M14b` session/report observations.
   - `M14b` host session/report observers run strictly read-only with bounded timeouts; if local `aplexer` inspection or report reading fails on a host, the host reports `health_state: 'uncertain'` / `report_status: 'unavailable'` without blocking heartbeats or mutating sessions.
   - `M14c` deployment status defaults to `deploy_state: 'unknown'` until an authenticated deployment/rollback receipt is recorded, preventing false `deployed: true` claims.
4. **Gate 3 — Cross-Host Runtime Acceptance vs Unit Tests**:
   - Local `vitest` and `python3 -m unittest` suites verify `M13-IG1`..`M13-IG3` and `M14-IG1`..`M14-IG4` at source level, but **runtime acceptance** of `M13` and `M14` requires live multi-host evidence (`Hetzner` + `botbox` + enrolled fleet hosts) verified by an independent model reviewer and acknowledged by live ops custody.

---

## 9. Inspection Commands, Containment, and Resource Receipt

### 9.1 Read-Only Inspection Commands Executed

1. `a whoami --json > .local/review/first-action-worker.json` and `a context` (first tool actions in own process; verified worker identity `platform-quota-status-plan-review` / `1f2004e9-3d3f-45f1-926e-1875058539e8`).
2. `a message send --workspace /home/alexey/git/agent-coordination --to platform ...` (sent task ACK `01a14968-f98b-7551-9843-2c68f0e75c65` and acknowledged coordinator reply `01a14969-669c-73e1-9939-0609baad5f13`).
3. Read `/home/alexey/tmp/agent-reports/hub-platform-brief.md` first, then `/home/alexey/tmp/agent-reports/hub-intake-brief.md` (Base + Addenda 1–4), then applicable `AGENTS.md` and reviewer role docs.
4. Inspected authoritative coordinator pin `6750e34bc2fdff07c84e0127c973ac46210f9e5d` via `git -C /home/alexey/git/agent-coordination show 6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and `STATUS.md`, plus parent commit diffs `7555734..6750e34`.
5. Inspected Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb` docs, configs, Worker/DO TypeScript sources (`src/quota.ts`, `src/routing.ts`, `src/model-routing.ts`, `src/fleet.ts`, `src/intake.ts`, `src/hub.ts`, `src/schema.ts`, `src/checks.ts`, `src/liveness.ts`), Python host sources (`host/agent_hub_host/quota_snapshot.py`, `fleet.py`, `cli.py`, `intake.py`, `oversight.py`, `oversight_adapter.py`, `task_authority.py`), and `public/fleet.html` + `public/fleet.js`.
6. Computed exact git blob SHAs and SHA-256 digests across all 29 reviewed artifacts/files (`.local/review/inspected-hashes.json`). No product tests, builds, dependency installs, or browser tools were run during this document review.

### 9.2 Cgroup Containment and Scratch Usage Receipt

- **Cgroup path**: `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/agent-platform-quota-status-plan-review-1776420735.scope`
- **Configured hard limits**:
  - `memory.max`: `1,572,864,000` bytes (`1500 MiB`)
  - `pids.max`: `256` tasks
  - `cpu.max`: `100000 100000` (`1.0` CPU quota)
- **Sampled observations during review** *(point samples and kernel cgroup watermarks observed during this document review; not claimed as continuous sub-millisecond telemetry)*:
  - `memory.current`: `~193.2 MiB` (`193,167,360` bytes)
  - `memory.peak`: `~218.7 MiB` (`218,652,672` bytes, well within the `1500 MiB` cap)
  - `pids.current`: `13` / `pids.peak`: `14` (well within the `256` tasks cap)
  - `.local/` aggregate disk usage: `~477 KiB` (`477,166` bytes before final receipt write, well below the `3 MiB` preventive guard and `4 MiB` hard cap)
- **Owned-file discipline**: Only `docs/reviews/QUOTA-STATUS-PLAN.md` is tracked and committed on sole parent `6bc91a1278250b374ea1d0b11acd353fae51a8cb`; private review receipts reside strictly under `.local/review/` and `.local/tmp/`.
