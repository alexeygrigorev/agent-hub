# Independent Argon Plan Review: M13 Quota Dashboard & Routing Admission and M14 One-Call Status (`6750e34bc2fdff07c84e0127c973ac46210f9e5d`)

- **Reviewer (R2 Correction)**: `platform-quota-status-plan-review-r2` (`gemini-4-argon-high`, native session `5de3093f-a3a3-48d8-b400-1b00e84899ff`)
- **Prior Reviewer (R1 Preserved Parent)**: `platform-quota-status-plan-review` (`gemini-4-argon-high`, native session `1f2004e9-3d3f-45f1-926e-1875058539e8`, immutable commit `b033eeade5b6718e15c47df08ff8989f523cdfe9`, blob `22ed63917d1aa0f5c40c1f01df6c44e7a0059b3a`, SHA-256 `03e2fda8bf5b7aee588c4edfebcb8ea31ed86ff9f93872f6b3a4637b96ca9d10`)
- **Agent Branches Task / Branch**: `0432` / `platform-qstatus-0432`
- **Review Worktree**: `/home/alexey/git/agent-hub-wt-platform-quota-status-plan-review`
- **Hub Base Commit**: `6bc91a1278250b374ea1d0b11acd353fae51a8cb` (`docs: correct independent review Git metadata before publication [skip ci]`)
- **Coordinator Integration Owner**: `platform` (`codex`, session `e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363`) in `/home/alexey/git/agent-coordination`
- **Authoritative Reviewed Coordinator Pin**: `6750e34bc2fdff07c84e0127c973ac46210f9e5d` (`docs: checkpoint hub status scope and active repair reviews [skip ci]`)
- **Reviewed Coordinator Artifacts**:
  - `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` (git blob `e058f3ca6264cb64b2d1d581e70c8fc04ff4c37b`, `50,239` bytes, `727` lines, SHA-256 `aeeeab5803cda380764901aa3b1689a57fb629ced57b0b33314becf2a669885c`)
  - `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/STATUS.md` (git blob `e40b8fbc41fb17a7c530e915fcecb4106fd74049`, `106,065` bytes, `1,662` lines, SHA-256 `0c6fa184f4cb8db08b1b9e5fcfc305af35c38d3876f16a6c2a42ca44a3ecd38c`)
- **Authoritative Operator Briefs Read First**:
  1. `/home/alexey/tmp/agent-reports/hub-platform-brief.md` (`2,403` bytes, `10` lines, SHA-256 `b13681ab123336d42399ef4c924fdc9d3009f56420ae43a437f4925886f4e0f7`)
  2. `/home/alexey/tmp/agent-reports/hub-intake-brief.md` (`3,805` bytes, `13` lines, SHA-256 `af48f48242bad7235a3a8a652b67e1694e816c1a935ba4f1657b903ae1d2b7f1`, Base Brief + Addenda 1–4)
- **Prior Preserved Argon M12 Plan Review**: `docs/reviews/INTAKE-TASKS-PLAN.md` (introduced in Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb` history by `1f000fea476cc62ec0b98cc4764e96b6d364bbad` `docs(review): review M12 intake assignment backfill handoff and session plan [skip ci]`, refined in `a13d227c6486221b5fb5f8b31948648cdea1f20d` `docs: refine M12 intake plan review contracts and verify 27e4b32 [skip ci]` and merged in `037bf039ec5ae01799a1cc863d31178a76107cdf` referencing coordinator commit `27e4b32487eb556cab624dfcc638cfa43faa0dd1` `docs: apply intake review contracts and assign metrics repair [skip ci]`; blob `ccb96d07b8dfa99ade9af09880e8a230ae64c7a3`, SHA-256 `9223d3e93d8e5e89e4ac37eac1f9ec71e0d5934e35f0f75e7dcea08adc1ec7e4`)
- **Review Date**: `2026-10-17`
- **Verdict**: **SCOPED ACCEPT** (plan-only on exact coordinator pin `6750e34bc2fdff07c84e0127c973ac46210f9e5d`, subject to binding implementation and acceptance gates `M13-IG1`..`M13-IG3` and `M14-IG1`..`M14-IG4` and preserving `M12e` Priority 1 -> `M12b/c` Priority 2 ordering; **zero implementation, source, or runtime acceptance is granted by this review**)

Review: approved sha=6750e34bc2fdff07c84e0127c973ac46210f9e5d reviewer=platform-quota-status-plan-review-r2/gemini-4-argon-high

---

## 1. Executive Summary and Scoped Verdict

Pinned coordinator commit `6750e34bc2fdff07c84e0127c973ac46210f9e5d` extends `docs/platform/PLAN.md` and `docs/platform/STATUS.md` with two new milestones derived from Alexey's full intake brief (`/home/alexey/tmp/agent-reports/hub-intake-brief.md`, Addenda 3 and 4) while preserving the platform consolidation baseline (`/home/alexey/tmp/agent-reports/hub-platform-brief.md`) and earlier accepted `M12` plan contracts (`docs/reviews/INTAKE-TASKS-PLAN.md`):

1. **`M13 — Quota dashboard and routing admission` (`PLAN.md:549–604`)**, split into `M13a` (host collection and authenticated quota snapshots), `M13b` (Hub API, `hub quota` CLI, and `dakit` dashboard view), and `M13c` (quota-aware routing and launch admission).
2. **`M14 — Status in one call` (`PLAN.md:606–685`)**, split into `M14a` (canonical status contract, API, and `hub status` CLI), `M14b` (every-host per-task session health and task-linked report headline/status provenance), `M14c` (landed-`main` vs currently active effective deployed revision including rollbacks and failed/pending deploys, independent CI state, and pending Alexey actions/approvals/device-login codes), and `M14d` (`--since` window and bounded caller-scoped diff since the caller's last status check).

### Why the Verdict is `SCOPED ACCEPT` (and Not `REVISE` on the Plan Pin)

Every substantive requirement in `hub-platform-brief.md` and `hub-intake-brief.md` (Base + Addenda 1–4)—including the deployment-inclusion and rollback refinements landed in coordinator commit `7d8d0f7d93fa7624c232b871b4d6766766786032` and checkpointed in `6750e34bc2fdff07c84e0127c973ac46210f9e5d`—is explicitly represented in `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and `STATUS.md`:

- **Primary objective and operator ordering preserved**:
  1. **Priority 1 is `M12e`** (`PLAN.md:41–44, 90, 107, 426–435, 698–705`): delivering the first working Hub session-control loop (`list`, `start` with `engine/tier/host/cwd/brief`, `send/prompt` with confirmed delivery, `capture/read`, `stop`, replacing `psc`) verified end-to-end from the `Grok Bot` box to a real throwaway session on Hetzner.
  2. **Priority 2 is `M12b/c`** (`PLAN.md:42–43, 107, 407–424, 706–708`): actual task assignment through existing routing with automatic session linkage, real Hetzner session/report backfill, and fresh-agent continuation from `hub handoff`.
  3. **Targeted `M9` host-auth dependency vs full `M9` independence** (`PLAN.md:57–64, 82–85, 698–701, 716–720`): Only the `M9` host-authority / enrollment approval-binding fix (`35047954f4711d96f3becfbddf5332388958431c`, plus its two HTTP negative repairs) is a shared dependency for `M12e` host authentication; the full `M9` accounting producer DTOs, 24-hour UTC buckets, `dakit` metrics UI, and producer adoption are not a blanket prerequisite blocking `M12b/c`, `M13`, or `M14`.
  4. **Spare-capacity parallel contract/source work for `M13` and `M14`** (`PLAN.md:90, 599–604, 679–685, 709–713`): Independent `M13` and `M14` contract/source development may proceed on spare worker capacity with disjoint file ownership after staffing `M12e`, without preempting `M12e` / `M12b/c` and without waiting forever for `M12` physical operations/enrollment ACKs (while actual multi-host rollout/runtime acceptance remains gated on host-owner rollout and enrolled machines).
- **All seven prior `M12` Argon contract requirements preserved**: `PLAN.md:468–539` retains items `1` through `7` (`M12-C1`..`M12-C7`) from `docs/reviews/INTAKE-TASKS-PLAN.md` (`1f000fea476cc62ec0b98cc4764e96b6d364bbad` / `a13d227c6486221b5fb5f8b31948648cdea1f20d`).
- **No parallel tracker, controller, bus, or second quota/status store**: `M13` and `M14` extend the existing Cloudflare Worker + single SQLite Durable Object (`src/hub.ts`), existing quota/fleet/routing/intake modules (`src/quota.ts`, `src/fleet.ts`, `src/routing.ts`, `src/model-routing.ts`, `src/intake.ts`), and existing Python stdlib host agent/CLI (`host/agent_hub_host/`).
- **Critical operational invariants are already in the pinned plan text**:
  - `M13` explicitly requires provider-native units (`percent_remaining`, reset timestamps, Codex credits, OpenCode Go spend), forbids fabricating dollar/token values from percentages, forbids uploading raw OAuth tokens/cookies/keys/prompt history, fails closed on stale/missing/unknown quota during routing and launch claim, preserves the 15% Codex and 5% Grok reserves across available applicable windows, deduplicates shared accounts via canonical verified Hub account records, and states that a passed `reset_at` timestamp never implies capacity recovery without a fresh post-reset observation (`PLAN.md:556–604`).
  - `M14` explicitly requires every-host per-task session health (`working`, `waiting`, `stopped`, `limit-hit`, `stuck`, `uncertain`), read-only detection of `Weekly/Monthly Limit Exhausted`, low-context warnings, and typed-but-unsubmitted composer drafts (**never** sending `Enter`, keystrokes, or auto-restarts from status collection), bounded task-linked report headline/verdict provenance distinguishing author claims from independent review verdicts, landed `main` vs **currently active deployed revision** (handling rollbacks and newer failed/pending deploys without false-positive deployed flags), CI reported separately from deploy, pending Alexey approvals/decisions/device-login codes (with `user_code` restricted to the authorized owner and tokens never shown), and bounded caller-scoped status diffs (`PLAN.md:606–685`).

### Why Approval is Scoped to Seven Concrete Source-Level Implementation Gates (`M13-IG1`..`M13-IG3`, `M14-IG1`..`M14-IG4`)

Comparing `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` against the actual Hub codebase at `6bc91a1278250b374ea1d0b11acd353fae51a8cb` surfaced seven concrete code-level mismatches between current `main` implementations and the planned `M13`/`M14` contracts. None of these require rewriting `PLAN.md` before implementation starts, **provided** the implementers and reviewers of `M13a–c` and `M14a–d` treat findings **`F-M13-1`..`F-M13-3`** and **`F-M14-1`..`F-M14-4`** (Section 5 and Section 6 below) as mandatory acceptance gates:

1. **`F-M13-1` / Gate `M13-IG1` (Medium — Quota DTO & Canonical Single-Store Bridging)**: `src/quota.ts` (`QUOTA_SCHEMA_version = "1.0"`, `L36–59, L420–483`) currently rejects any window field outside `["percent_remaining", "reset_at", "present", "rolling", "error"]` (`400 unknown_window_field`), and `src/fleet.ts:26–38, 80–94` strips `fleet_subscriptions.quota_json` (even dropping window `error`) while `FleetService.observation()` ignores `host_quota_snapshots` whenever `route.profile !== null` or `COUNT(DISTINCT r.account) !== 1` on `(machine_id, provider)`. `M13a/b` must extend the sanitized contract in the existing stores (no second quota store) to carry Codex credits, OpenCode Go native spend, `collector_version`, and profile/account qualification (`zcodex`, `muse`).
2. **`F-M13-2` / Gate `M13-IG2` (Medium — Multi-Host `GET /api/quota`, `hub quota` CLI & Canonical `(provider, account_label)` Pool Deduplication)**: `src/quota.ts:924–1000` currently restricts `GET /api/quota` to a host reading its own `machine_id` or a browser admin session supplying a mandatory single `?host_id=<id>`, rejecting `agent`, `routing-client`, and admin bearer tokens (`403`), and `host/agent_hub_host/cli.py` has no `hub quota` command. `M13b` must add `hub quota` and an authenticated fleet-wide/filterable quota view that deduplicates shared pools strictly by canonical verified Hub account identity `(fleet_accounts.provider, fleet_accounts.label)` and verified `fleet_routes`, never colliding arbitrary unqualified labels across providers or accounts.
3. **`F-M13-3` / Gate `M13-IG3` (High — Fail-Closed Routing Admission, Applicable vs Benignly Absent Optional Windows, and No Manual Safety Override)**: `src/routing.ts:106–107` and `src/model-routing.ts:22–26` currently do **not** check `subscription.observed_at` or `quota.observed_at` freshness (`<= 1200 s`), ignore `quota.status !== 'ok'`, `limit_reached !== false`, and `stale === true`, strip/ignore sibling window `error: 'unavailable'`, and do not reconcile passed `reset_at` timestamps against `observed_at`. `M13c` must close these gaps fail-closed while preserving benignly absent optional/unsupported windows (`present: false, percent_remaining: null, reset_at: null` without error, e.g. Codex `5h`/`monthly` when only `7d`/`weekly` is supported), enforcing Codex 15% and Grok 5% reserves across all available applicable (`present: true`) windows, re-verifying at `FleetService.claim()`, and disallowing any manual quota-safety bypass.
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
| `58885564b5f24dd8652a3ffa4f0f1550a9f4f7e3` | `5888556` | `docs: record session repair and independent metrics R4 review [skip ci]` | Recorded `M12e` R2 repair candidate `2ca4700a80af316538319da462a4c17dac17867a` and `M9` R4 review findings |
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
| :--- | :--- | ---: | :--- | :--- |
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
| `public/fleet.html` | `10,093` | `86` (`85` LF) | `8741a8cf925264712fb7cc8c504ca61d4b9be2f1` | `be30ad8043ba51d70f4cf6d151eeebb9221b0bdeff50682f722286fe091530fe` |
| `public/fleet.js` | `12,113` | `119` | `3bd6065186e3dbc9cac7992eae08231dab51f6d9` | `902f8d9632830ee655c79c41c1c0e3d9452b51f50bb769fc02a28147aa4c85e6` |
| `test/quota.test.ts` | `63,386` | `1,482` | `22462afde60cdb623a1f5e1efaa850363a03f2d2` | `8e0881d9fbffb8a647a84a3bb8058449e04e2f0ab46633852f9486cde7b36036` |
| `test/fleet.test.ts` | `17,216` | `209` | `d61e655a51e9fe011f1af1dc72dfff7fc25a9753` | `67916cce1870ed262575bfccac27011a2df424044750c26b94cad4b04b8b760f` |
| `test/routing.test.ts` | `22,079` | `242` | `664a66c829c64d382a199a3c3162903504def087` | `b4e48cff6c5d633cad634729ccca717152260709d45b95cb1a81ae969854e3a4` |
| `host/tests/test_quota_snapshot.py` | `37,865` | `809` | `9fb283464ebd89d73d34f425eea48761a0e76946` | `171b45f7c744b07e4be6a96adfacdb946bd22c645ea7d78dc87153a75edd9d87` |
| `host/tests/test_fleet.py` | `14,566` | `211` | `cb4b13da32e8cf7849202020a7870feade541ac6` | `428f4883f02da5581f817b47499d199916767ed9736a5e0e025406a94d47786a` |

### 2.4 Strict Source vs Runtime Non-Acceptance Ledger

To prevent any conflation of plan approval with implementation or runtime readiness, the exact acceptance state as of `6750e34bc2fdff07c84e0127c973ac46210f9e5d` and Hub `main` `6bc91a1278250b374ea1d0b11acd353fae51a8cb` is:

| Slice / Capability | Hub `main` (`6bc91a1`) Source State | Candidate / Branch State | Live Cross-Host Runtime State |
| :--- | :--- | :--- | :--- |
| **`M4` Task/outcome links, `M5` Scheduled follow-ups & conversation retention repair, `M7` Configured roles & epoch-fenced job effects** | Source-only accepted on `main` (`aec97138afd5bccd9cd053794d85ee6dcfef548f` / `4c7cf04bd689b81c867bd2622f2c116bede9d94a` / `6bc91a1278250b374ea1d0b11acd353fae51a8cb`) | Landed on `main` | **Not runtime-accepted** (`M5`/`M11` physical host acceptance and `M8b` qualified effect backend remain open) |
| **`M10` Request-to-outcome follow-through in conversations & `M11` Supported host adoption/migration** | **Not accepted** (`PLAN.md:341–342, 721–723`) | Open roadmap milestones | **Not accepted** |
| **`M12a` Intake API, CLI & `hub handoff` source** | Source-only accepted on `main` (`adae0fb346028c449db7ce6b5858720523ffd6a9` / `c290454e88c49c6fe863d20dd2717d58522851d0` / `bc796013d9f522d8a2e3ea050f7b264e2fabfc41` / `6bc91a1278250b374ea1d0b11acd353fae51a8cb`) | Landed on `main`; `M12b/c` actual assignment, real backfill, and fresh-agent `hub handoff` adoption remain open | **Not runtime-accepted** (no live Botbox -> Hetzner AI loop or fresh-agent handoff proven) |
| **`M12e` Native `aplexer` session control replacing `psc`** | **Not on `main`** | R2 candidate `2ca4700a80af316538319da462a4c17dac17867a` under active Argon review (`platform-session-r2-review` / `e0fb234a`) | **Unaccepted**; true `Grok Bot` box -> Hetzner loop, full tier/cwd coverage, and actual handoff are not accepted |
| **`M9` Host-authority fix & accounting metrics ingestion** | **Not accepted** (R1–R4 received `REVISE`) | Core host-authority commit `35047954f4711d96f3becfbddf5332388958431c` has two new HTTP negative cases under repair/review; full R5 under review (`platform-metrics-r5-review`) | **Unaccepted** (only host-auth is a shared `M12e` dependency; full `M9` accounting DTO/UI is independent) |
| **`M13` Quota dashboard & routing admission** | Existing `M3`/`M7` baseline only (`src/quota.ts`, `src/fleet.ts`, `src/routing.ts`, `src/model-routing.ts`) | **Planned in `6750e34`** (`M13a–M13c`) | **Not implemented / not accepted** |
| **`M14` One-call `hub status`** | Stub `hub status` local-config print only (`host/agent_hub_host/cli.py:44–54`) | **Planned in `6750e34`** (`M14a–M14d`) | **Not implemented / not accepted** |

---

## 3. Requirement-by-Requirement Audit Against `hub-platform-brief.md` and `hub-intake-brief.md` (All Four Addenda)

### 3.1 Platform Consolidation Brief (`/home/alexey/tmp/agent-reports/hub-platform-brief.md`)

| Brief Requirement | Pinned Plan Location (`6750e34:docs/platform/PLAN.md`) | Hub `6bc91a1` Source Baseline | Audit Verdict |
| :--- | :--- | :--- | :--- |
| Hub is the single durable control plane for agent registry, liveness, conversations, task/launch records, quota snapshots, and supervision; `aplexer` is the per-machine execution plane | `L1–31`, `L95–110`, `L363–466`, `L549–685` | `src/hub.ts`, `src/fleet.ts`, `src/quota.ts`, `src/intake.ts` | **SATISFIED IN PLAN** |
| Retire standalone daemons (`desktop-orchestrator`, `agent-dashboard`, `principal-ping`, `supervision`, bus `:8788`) only after replacement slices pass migration gates | `L26`, `L338–361`, `L721–723` | Preserved in `docs/SPEC.md` §12 and `PLAN.md` (`M8b`/`M11`) | **SATISFIED IN PLAN** |
| Cloudflare Workers Free plan design & `<= USD 5/month` total cloud budget, bounded SQLite DO storage, no unauthorized deploys | `L14–25`, `L556–575`, `L614–621`, `L651–659` | Bounded DO row/payload/write limits in `src/schema.ts`, `src/quota.ts`, `src/fleet.ts`, `src/intake.ts`; total account-wide Cloudflare billing cap (`<= USD 5/month`) is an operational/no-deploy policy gate (`AGENTS.md:28–29`), not enforced by application code alone | **SATISFIED IN PLAN** |

### 3.2 Intake, Handoff, Session Control, Quota, and One-Call Status Brief (`/home/alexey/tmp/agent-reports/hub-intake-brief.md`)

| Brief Section & Verbatim Requirement | Pinned Plan Location (`6750e34:docs/platform/PLAN.md`) | Hub `6bc91a1` Source Observation & Gap | Audit Verdict |
| :--- | :--- | :--- | :--- |
| **Base Brief (`L1–5`)**: Capture all intake (chat, voice notes, forwarded asks) as tracked tasks (`M12a`); token/device-auth `hub` CLI + API (and later MCP at `mcp.dtcdev.click`) to create/list/filter/show/update/link and **assign** work (`engine/tier/host`) via existing routing (`M12b`); **backfill** current Hetzner `aplexer` sessions and `~/tmp/agent-reports/*.md` (`M12c`) | `M12` (`L363–424, L437–452, L468–526`), Next actions (`L706–708`) | `M12a` source landed (`src/intake.ts`, `host/agent_hub_host/intake.py`); `M12b` real routing assignment and `M12c` live backfill/adoption remain Priority 2 right after `M12e` | **SATISFIED IN PLAN** (`M12b/c` Priority 2 after `M12e`) |
| **Addendum 1 (`L7`) — Handoff Context**: Per-task verbatim requests, decisions, standing rules (allowed engines, push-to-main, `[skip ci]`, no public posts, sandbox-only AWS `817685572750`), current state, next step, sessions/hosts, reports, commits, pending approvals; global `hub handoff` self-contained briefing so any fresh agent on any host can continue | `L379–397`, `L407–416`, `L446–451`, `L490–500` (`3. Handoff fencing`), `L706–708` | `M12a` landed CAS-fenced handoff source in `src/intake.ts` and `host/agent_hub_host/intake.py`; live fresh-agent `hub handoff` continuation proof remains open in `M12b/c` | **SATISFIED IN PLAN** |
| **Addendum 2 (`L9`) — Hub Replaces `psc` (`M12e` Priority 1)**: Orchestrate sessions across `hetzner`, `laptop`, `win35` (`list`, `start` `engine/tier/host/cwd/brief`, `send/prompt`, `capture/read`, `stop`) with reliable delivery confirmation; `psc` backup only; prove full `start -> send -> confirm-delivered -> capture -> stop` loop from `Grok Bot` box to Hetzner throwaway session | `L41–44`, `L90`, `L107`, `L426–435`, `L453–462`, `L527–539` (`7. Session control`), `L698–705` | R2 candidate `2ca4700a80af316538319da462a4c17dac17867a` under active Argon review (`e0fb234a`); shared `M9` host-auth fix `35047954f4711d96f3becfbddf5332388958431c` has two HTTP negative cases being repaired; live Botbox -> Hetzner loop not yet accepted | **SATISFIED IN PLAN** (`M12e` kept Priority 1) |
| **Addendum 3 (`L11`) — `M13a`**: Collect & normalize usage/quota across every enrolled host (`Hetzner`, `ovh2`, `mac`, `win`, `botbox`) for **Codex** (weekly remaining %, credits), **Z.ai** (5h/weekly windows, resets), **Claude**, **Grok**, **Antigravity/Gemini**, and **OpenCode Go** (spend + Muse Spark profile); preserve provider-native units; never fabricate dollar/token counts from %; never upload raw OAuth tokens/cookies/keys | `M13a` (`L556–568`, `L587–590`, `L599–604`) | `host/agent_hub_host/quota_snapshot.py` & `src/quota.ts` only accept `percent_remaining` in schema `"1.0"` and lack Codex credits, OpenCode Go spend, `collector_version`, and profile keys in `host_quota_snapshots` | **SATISFIED IN PLAN** — requires **`M13-IG1`** |
| **Addendum 3 (`L11`) — `M13b`**: Expose quota in Hub API, `hub quota` CLI, and `dakit` UI; show per-host and shared-account pools without double-counting; distinguish `fresh`, `stale`, `exhausted`, `unavailable`, `unknown`; protect private usage/account inventory with Hub auth | `M13b` (`L570–575`, `L590–592`, `L599–604`) | `GET /api/quota` (`src/quota.ts:924–1000`) is single-host only (`?host_id=`), rejects `agent`/`routing-client`/admin bearer tokens, and `cli.py` has no `hub quota` subcommand | **SATISFIED IN PLAN** — requires **`M13-IG2`** |
| **Addendum 3 (`L11`) — `M13c`**: Routing & launch admission must consume normalized quota snapshots, exclude exhausted engines/accounts/windows, fail closed on stale/missing/unknown quota, re-check at launch claim, and never treat a passed `reset_at` timestamp alone as recovered capacity | `M13c` (`L577–585`, `L592–597`, `L599–604`) | `src/model-routing.ts:22–26` & `src/routing.ts:106–107` skip subscription freshness checks, strip/ignore sibling `unavailable` windows in `fleet.py`/`fleet.ts`, ignore `reset_at` vs `observed_at`, and need unified claim-time re-check | **SATISFIED IN PLAN** — requires **`M13-IG3`** |
| **Addendum 4 (`L13`) — `M14a`**: One-call `hub status` (`hub status`, `hub status --since 2h`, `hub status --json`) + authenticated API joining intake tasks, sessions on every host, reports, Git/CI/deploy state, and pending human actions; partial coverage when a host is offline/stale; per-section source/time/freshness | `M14a` (`L614–621`, `L661–664`, `L679–685`) | `host/agent_hub_host/cli.py:44–54, 343–344` currently binds `hub status` to a local-config print stub; no `/api/status` route exists yet | **SATISFIED IN PLAN** — requires **`M14-IG1`** |
| **Addendum 4 (`L13`) — `M14b`**: Every-host per-task session status (`native_id`, host, engine/profile, `working`/`waiting`/`stopped`/`limit-hit`/`stuck`/`uncertain`); detect `Weekly/Monthly Limit Exhausted`, low-context warnings, and typed-but-unsubmitted composer drafts; **status collection is strictly read-only and never submits `Enter` or restarts jobs**; task-linked report headline/status line with author-claim vs independent-review provenance; no raw prompt history/drafts/secrets uploaded | `M14b` (`L623–635`, `L664–668`, `L679–685`) | `PUT /api/fleet/sessions` (`src/fleet.ts:284–309`) only stores basic `aplexer list` fields; no session limit/context/draft or report-headline metadata exists yet | **SATISFIED IN PLAN** — requires **`M14-IG2`** |
| **Addendum 4 (`L13`) + Coordinator `7d8d0f7` — `M14c`**: Landed `main` commits vs deployed commits where `deployed` requires a successful deploy run whose **currently active deployed revision** includes the commit; rollbacks mark rolled-back commits as no longer deployed; newer failed/pending deploys do not replace the last confirmed active deployment; CI reported separately; pending Alexey approvals/decisions/device-login codes (`user_code` only to authorized owner, never tokens) | `M14c` (`L637–649`, `L668–675`, `L679–685`) | No Git/CI/deployment ledger exists in `src/schema.ts` yet; `device_pairings` (`src/schema.ts:27–44`) stores `user_code` and `device_code_hash` | **SATISFIED IN PLAN** — requires **`M14-IG3` & `M14-IG4`** |
| **Addendum 4 (`L13`) — `M14d`**: `--since <window>` (e.g. `2h`) and short caller-scoped diff since the caller's last status check; bounded auth-scoped cursors/snapshots; expired/unavailable baseline reported honestly without inventing diffs | `M14d` (`L651–659`, `L673–675`, `L679–685`) | No caller-scoped status watermark table exists in `src/schema.ts` yet | **SATISFIED IN PLAN** — requires **`M14-IG4`** |

---

## 4. Priority Discipline, Architecture, and Milestone Slicing Review

### 4.1 Exact Operator Priority (`M12e` First -> `M12b/c` Second), Targeted `M9` Host-Auth Dependency, and Spare-Capacity Parallelism for `M13`/`M14`

Pinned `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` aligns with Alexey's briefs without over-serializing independent work:

1. **Primary execution priority (`M12e` first, then `M12b/c`, `PLAN.md:41–44, 107, 698–708`)**:
   - **Priority 1 (`M12e`)**: Complete the active Argon R2 review (`e0fb234a`) of session repair `2ca4700a80af316538319da462a4c17dac17867a`, repair any findings, compose with the accepted host-authority fix, and deliver the first real working `M12e` loop (`start -> send -> confirm-delivered -> capture -> stop` from the `Grok Bot` box to Hetzner, preserving full `engine/tier/host/cwd/brief` scope and safe composer checks).
   - **Priority 2 (`M12b` & `M12c`)**: Immediately follow with `M12b` task assignment through existing routing (proving automatic real-session linkage) and `M12c` actual Hetzner session/report backfill and fresh-agent continuation via `hub handoff`.
2. **Targeted `M9` host-authority dependency vs full `M9` accounting (`PLAN.md:57–64, 82–85, 698–701, 716–720`)**:
   - Only the **host-authority / enrollment approval-binding fix** (`35047954f4711d96f3becfbddf5332388958431c`, where two HTTP negative cases are currently being repaired) is a real dependency for `M12e` host authentication.
   - The remainder of `M9` (accounting producer DTOs, 24-hour UTC buckets, `dakit` metrics UI, and producer adoption) is an independent workstream and **must not** be turned into a blanket prerequisite blocking `M12b/c`, `M13`, or `M14`.
3. **Spare-capacity contract/source parallelism vs physical rollout dependency (`PLAN.md:88–90, 599–604, 679–685, 709–713`)**:
   - Once primary `M12e` and `M12b/c` work is staffed, independent `M13` (`M13a–c`) and `M14` (`M14a–d`) contract and source work may proceed in parallel on spare worker capacity with disjoint file ownership **without** preempting primary `M12` work and **without** waiting forever for `M12` physical operations/enrollment ACKs.
   - Actual multi-host runtime rollout and acceptance of `M13` and `M14` still retain their real dependencies on host-owner rollout, enrolled hosts, and accepted upstream contracts.

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

### Finding `F-M13-1` (Medium — Binding Gate `M13-IG1`): Quota DTO Schema Version `1.0` & Split `host_quota_snapshots` vs `fleet_subscriptions` Storage Drop Native Units, Window Errors, and Profile Routes

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
1. **Strict schema `1.0` field rejection in `src/quota.ts`**:
   - `src/quota.ts:36–59` defines `QUOTA_SCHEMA_VERSION = "1.0"`, `ALLOWED_TOP_KEYS`, `ALLOWED_PROVIDER_KEYS = ["status", "observed_at", "windows", "details"]`, and `ALLOWED_WINDOW_KEYS = ["percent_remaining", "reset_at", "error"]`.
   - `src/quota.ts:365–379` (`validateWindow`) rejects any window object containing keys outside `ALLOWED_WINDOW_KEYS` with `400 unknown_field`, and requires `percent_remaining` to be `number | null` in `[0, 100]`.
   - `src/quota.ts:440–445` (`validateDetails`) restricts `details` to `limit_reached`, `stale`, and (for `grok` only) `has_grok_code_access`.
   - Consequently, `src/quota.ts` **cannot currently ingest** Codex credit balances, OpenCode Go spend in native units, `collector_version`, or per-profile metadata (`muse` vs other OpenCode Go profiles, `zcodex` vs `codex`).
2. **Host collector and fleet adapter gaps in `host/agent_hub_host/quota_snapshot.py` and `host/agent_hub_host/fleet.py`**:
   - `quota_snapshot.py:134–425` collects `claude`, `gemini`, `codex`, `grok`, `zai`, and `go` (`opencode`), normalizing every provider window solely to `percent_remaining` (`0..100`) and `reset_at`. It does not emit `collector_version`, Codex credit balances, or OpenCode Go native spend fields.
   - `host/agent_hub_host/fleet.py:124–143` (`quota_metadata`) copies only `percent_remaining`, `reset_at`, and `present` into each subscription window (`lines 136–140`), dropping `w.get("error")` as well as any native-unit/collector metadata before `PUT /api/fleet/subscriptions`.
3. **Split storage and canonical account/profile mapping in `src/fleet.ts`**:
   - `host_quota_snapshots` (`src/quota.ts:24–33`) is keyed solely by `host_id TEXT PRIMARY KEY` (one JSON blob per machine, indexed by provider name `claude | gemini | codex | grok | zai | go`), whereas `fleet_accounts` (`src/fleet.ts:8–10, 238–260`) is the authoritative account registry (`id`, `label`, `provider`, `max_concurrency`) joined via `fleet_routes (machine_id, engine, account, profile)` (`src/fleet.ts:80–94, 263–282`) and `fleet_subscriptions (machine_id, account, service, ...)` (`src/fleet.ts:310–328`).
   - `FleetService.observation()` (`src/fleet.ts:80–98`) maps a host-level `host_quota_snapshots` row to a canonical account `(account_label, provider)` **only** when `route.profile === null` and `fleet_routes` proves a single unambiguous canonical account (`accounts.size === 1`) for `(machine_id, provider)`; otherwise (`route.profile !== null` or `accounts.size !== 1`), it falls back exclusively to `fleet_subscriptions`.
   - Meanwhile, `sanitizedQuota()` in `src/fleet.ts:26–38` strips `fleet_subscriptions.quota_json` down to `{status, observed_at, windows: {<k>: {present, percent_remaining, reset_at}}, details: {limit_reached, stale}}`, omitting both `w.error` and any native credit/spend/collector metadata.

#### Required Implementation & Acceptance Check (`M13-IG1`)
- **Contract adjustment for `M13a`**:
  1. Extend the sanitized quota contract (e.g. additive schema `"1.1"` backward-compatible with `"1.0"` readers, or explicitly allowlisted optional fields in `src/quota.ts`, `host/agent_hub_host/fleet.py:124–143`, and `sanitizedQuota()` in `src/fleet.ts:26–38`) to preserve `w.error` (`"unavailable" | null`) and allow bounded, secret-free provider-native usage fields:
     - `collector_version` (bounded ASCII string, e.g. `<= 64` chars) at the snapshot/subscription envelope.
     - Optional window/provider native-unit fields: `credits_remaining: number | null`, `spend_used: number | null`, `spend_limit: number | null`, `unit: 'percent' | 'credits' | 'usd_cents' | 'requests' | 'tokens' | null`, and `profile: string | null` (e.g. `opencode-go/muse-spark-1.3-contributor`, `zcodex`).
  2. Maintain strict fail-closed unknown-key rejection (`400 unknown_field`) and secret-key rejection (`access_token`, `refresh_token`, `id_token`, `api_key`, `cookie`, `authorization`, `session_token`, `email`, `account_id`, `organization_id`, `raw_output`).
  3. Reconcile `host_quota_snapshots` and `fleet_subscriptions` against the existing authoritative `fleet_accounts` (`(provider, label)`) and `fleet_routes` (`(machine_id, engine, account, profile)`) tables **without creating a second quota store**: profile-qualified routes (`zcodex`, `muse`) and multi-account hosts (`codex-1`, `codex-2`) must bind explicitly to their verified `(provider, account_label, profile)` route in `fleet_subscriptions`, while single-account host snapshots (`host_quota_snapshots`) map to an account label only when `fleet_routes` proves a single unambiguous canonical account (`accounts.size === 1`, `profile === null`) for `(machine_id, provider)`. Never fabricate dollar or token amounts from percentage-only windows.
- **Executable verification gate**: Extend existing suites in `test/quota.test.ts` (currently covering schema/window rules at `L595, L715, L1414`), `test/fleet.test.ts` (`L27–35, L82–100, L139–156, L185–193`), `host/tests/test_quota_snapshot.py` (`L157–194, L259–278`), and `host/tests/test_fleet.py` (`L25–47, L169–178`) to verify that: (a) all six provider families (`codex`, `zai`, `claude`, `grok`, `gemini`, `go`) round-trip native units, `w.error`, and `collector_version` through host collection and Hub ingestion, (b) any raw token/cookie/email/unknown key is rejected with `400`, and (c) percentage-only windows never synthesize dollar or token numbers.

---

### Finding `F-M13-2` (Medium — Binding Gate `M13-IG2`): `GET /api/quota` Single-Host / Cookie-Only Read Restriction, Canonical Shared-Pool Deduplication, Missing `hub quota` CLI, and `dakit` UI Coverage

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
  1. Extend `GET /api/quota` (with optional `?host_id=`, `?provider=`, `?engine=`, `?account=`) to authenticate:
     - Human admin sessions (`hub_session`) and admin bearer tokens (`HUB_TOKEN`) for full fleet-wide and per-host/per-account views.
     - Host bearer tokens (`role === "host"`) for their bound `machine_id` (preserving existing host isolation).
     - Authorized orchestrator/assistant tokens (verified against current active credential/family and `registry_incarnation` per `M12-C1`) for authorized quota summaries.
     - Unauthenticated or revoked/stale-incarnation callers must be rejected with `401`/`403`.
  2. **Canonical shared-pool deduplication (`src/fleet.ts:8–10, 80–94, 238–260, 318`)**: Deduplicate shared subscription pools across hosts (`Hetzner`, `ovh2`, `mac`, `win`, `botbox`) strictly by the canonical verified provider+account identity on `fleet_accounts` (`(provider, label)`, e.g. `("codex", "codex-1")`, `("zai", "zai-pro")`) joined with verified `fleet_routes (machine_id, engine, account, profile)`:
     - **Never collide arbitrary unqualified labels** across different providers or unverified accounts (e.g., an account label `"default"` or `"main"` under `claude` must never collide with `"default"` or `"main"` under `codex` or `gemini`, nor may an unverified host-supplied string merge into another account's pool).
     - A bare `account` label may alias to a shared pool **only** when resolved against `fleet_accounts` + `fleet_routes` as an authoritative, uniquely qualified `(provider, account_label)` canonical record (matching the existing `FleetService.observation()` invariant in `src/fleet.ts:87–91` and tested in `test/fleet.test.ts:27–35, 139–156, 185–193` and `test/routing.test.ts:166–188`).
     - A single shared canonical `(provider, account_label)` pool reported by multiple hosts is projected once as one shared capacity pool with per-host observation provenance (`observed_by_hosts`), never summed as double capacity.
  3. Add `hub quota [--host <id>] [--provider <p>] [--engine <e>] [--account <label>] [--json]` in `host/agent_hub_host/cli.py`.
  4. Update the `dakit` dashboard (`public/fleet.html` / `public/fleet.js` or dedicated quota section) to render native units (Codex weekly % + credits, Z.ai 5h/weekly + resets, OpenCode Go spend + Muse Spark profile, Claude, Grok, Antigravity/Gemini) and explicit `fresh | stale | exhausted | unavailable | unknown` state badges using `textContent` / DOM nodes only (preserving the `public/fleet.js:1` XSS invariant).

---

### Finding `F-M13-3` (High — Binding Gate `M13-IG3`): Routing Quota Admission, Benign Optional-Window Absence vs Sibling `unavailable` Errors, Reserve Enforcement, Passed `reset_at` Reconciliation, and Zero Manual Quota-Safety Bypass

#### Source Observation on Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`
Inspecting `docs/QUOTA.md` (§2.3, §3, §7.3), `src/quota.ts`, `src/routing.ts`, `src/model-routing.ts`, `src/fleet.ts`, and `host/agent_hub_host/fleet.py` revealed how window applicability vs error/staleness is currently handled and where fail-open gaps exist:

1. **Existing `docs/QUOTA.md` contract on benign absence of optional/unsupported windows vs genuine `error: "unavailable"`**:
   - Per `docs/QUOTA.md` §2.3 & §3 (`L64–86, L242–255`) and `src/quota.ts:819–830` (`projectWindowForRead`), a provider window has two distinct `present: false` representations:
     - **Benign absence (optional or unsupported window not reported by upstream)**: omitted from raw `windows` (or projected with `present: false, percent_remaining: null, reset_at: null` and **no** `error`). As `docs/QUOTA.md` §3 states, Codex `five_hour` and `monthly` may be absent when upstream reports only `seven_day` (`weekly`), and `FleetService.accountStatus()` (`src/fleet.ts:104–116`, tested in `test/fleet.test.ts:82–91`, `test/quota.test.ts:595, 715, 1414`, `host/tests/test_quota_snapshot.py:157–194, 259–278`, `host/tests/test_fleet.py:169–178`) rightly treats benignly absent optional windows (`present: false` without `error`) as neither `0%` nor an error, so a host with a valid weekly measurement (`> 15%` Codex reserve) remains eligible when policy allows.
     - **Genuine window failure (`error: "unavailable"`)**: when a supported/required window fails to parse or is unavailable, `src/quota.ts:825` projects `{present: false, percent_remaining: null, reset_at: null, error: "unavailable"}`.
2. **Defect A — `fleet.py:124–143` and `src/fleet.ts:26–38` strip `w.error`, and `modelCandidates()` (`src/model-routing.ts:22–26`) ignores `quota.status`, `limit_reached`, `stale`, and sibling `error: "unavailable"`**:
   - `host/agent_hub_host/fleet.py:136–140` (`quota_metadata`) and `src/fleet.ts:30–35` (`sanitizedQuota`) both omit `error` when copying windows into `fleet_subscriptions.quota_json`, turning a genuine `{present: false, error: "unavailable"}` failure on a `fleet_subscriptions` route into a silent `{present: false}`!
   - Furthermore, while `FleetService.accountStatus()` (`src/fleet.ts:104–116`) checks `q.status !== "ok" || q.details?.limit_reached !== false || q.details?.stale === true` and `windows.some((w) => w.error === "unavailable")`, `modelCandidates()` (`src/model-routing.ts:22–26`) performs **none of those checks**:
     ```ts
     const windows = Object.values(observation.quota?.windows ?? {}).filter(
       (window) => window.present !== false && Number.isFinite(window.percent_remaining),
     );
     const remaining = windows.length ? Math.min(...windows.map((window) => Number(window.percent_remaining))) : null;
     ```
   - Consequently, if a provider has two supported windows (`five_hour` with `error: "unavailable", present: false` and `seven_day` with `percent_remaining: 80`), or if `quota.details.limit_reached === true` / `quota.details.stale === true`, `modelCandidates()` ignores the broken sibling window and admits the route!
3. **Defect B — `RoutingService.dispatch()` (`src/routing.ts:106–107`) never checks `subscription.observed_at` or `quota.observed_at` freshness**:
   - `dispatch()` checks `fresh(machine.last_checkin_at, at)` (`<= 180,000 ms`), but parses `subscription.quota_json` without checking `freshQuota(subscription.observed_at, at)` (`<= 1,200,000 ms` / 20 minutes) or `quota.observed_at`. If a host heartbeat is fresh but its quota snapshot is hours old, `dispatch()` treats the stale `quota_json` as current.
4. **Defect C — Passed `reset_at` timestamps are not reconciled against `observed_at`**:
   - Neither `modelCandidates()` nor `FleetService.accountStatus()` compares `Date.parse(w.reset_at)` against `Date.parse(quota.observed_at)` and `now`. If a window was observed at `10:00Z` with `reset_at: 10:05Z`, and at `10:06Z` no post-reset snapshot (`observed_at >= 10:05Z`) has arrived yet, any pre-reset snapshot whose `reset_at` has passed is stale relative to its own window boundary until refreshed.
5. **Defect D — `FleetService.claim()` (`src/fleet.ts:225–240`) does not re-verify quota at launch claim time**:
   - `POST /api/fleet/launches/:id/claim` checks machine drain state and `activeCount >= account.max_concurrency`, but does not re-check `accountStatus()` or route reserve gates before transitioning an `offered` launch to `claimed`.

#### Required Implementation & Acceptance Check (`M13-IG3`)
- **Contract adjustment for `M13c` (with routing-owner coordination)**:
  1. **Preserve `w.error` end-to-end and share one canonical `evaluateQuotaAdmission(observation, reservePercent, nowMs)` helper** across `FleetService.accountStatus()`, `modelCandidates()`, `RoutingService.dispatch()`, and `FleetService.claim()`:
     - Reject (`status: 'unknown' | 'stale'`) if `!observation` or `ageMs(observation.observed_at, nowMs) > QUOTA_STALE_AFTER_MS` (`1,200,000 ms`) or `ageMs(observation.quota?.observed_at, nowMs) > QUOTA_STALE_AFTER_MS`.
     - Reject (`status: 'exhausted' | 'stale' | 'unknown'`) if `quota.status !== 'ok'`, `quota.details?.stale === true`, or `quota.details?.limit_reached !== false` (and for `grok`, require `quota.details?.has_grok_code_access === true`).
     - **Distinguish benign optional-window absence from genuine window error/unavailability**:
       - If **any** window in `quota.windows` has `w.error === 'unavailable'` (or any non-null `w.error`) or `(w.present === true && !Number.isFinite(w.percent_remaining))`, fail closed (`status: 'unknown'`). No unknown-required-window bypass is permitted.
       - If an optional or unsupported window (e.g. Codex `five_hour` or `monthly` when upstream reports only `seven_day`/`weekly`) is benignly absent (`w.present === false`, `w.percent_remaining === null`, `w.reset_at === null`, and `w.error == null`), ignore that absent window rather than treating it as `0%` or an error — keeping hosts with a valid weekly measurement eligible when policy allows.
     - Reject (`status: 'stale'`) if any applicable (`present: true`) window has a finite `reset_at` where `Date.parse(w.reset_at) <= nowMs` **and** `Date.parse(quota.observed_at) < Date.parse(w.reset_at)` (the reset boundary has passed without a post-reset observation `observed_at >= reset_at`).
     - **Apply reserve across all available applicable (`present: true`) windows**: Require `presentWindows.length >= 1`, compute `remaining = Math.min(...presentWindows.map(w => w.percent_remaining))`, and reject (`status: 'exhausted'`) if `remaining <= Math.max(0, reservePercent)` (enforcing Codex `15%` and Grok `5%` reserves across every available applicable window).
  2. Re-run `evaluateQuotaAdmission` inside `FleetService.claim()` (`POST /api/fleet/launches/:id/claim`) so a route whose quota became exhausted, stale, or unavailable while `offered` fails closed at claim admission.
  3. Ensure engine/model preference order (Muse Spark 1.3 `opencode-go/muse-spark-1.3-contributor` and Z.ai `zcodex` / Z.ai Flash `glm-4.7-flash` / `glm-4.5-flash` for lightweight tasks) is applied **only** among routes that pass `evaluateQuotaAdmission` and qualification gates.
  4. **No manual quota-safety override bypass**: No manual quota override may ever bypass `exhausted`, `unknown`, `stale`, reserve (`15%` Codex, `5%` Grok), or cooldown launch safety. Only read-only human inspection (`hub quota`, `hub status`, dashboard) and holding blocked work in `queued` / `waiting-on-human` are allowed.
- **Executable verification gate**: Add regression tests in `test/quota.test.ts`, `test/fleet.test.ts`, `test/routing.test.ts`, `host/tests/test_quota_snapshot.py`, and `host/tests/test_fleet.py` proving: (a) a Codex host with valid `seven_day` (`> 15%`) and benignly absent `five_hour`/`monthly` (`present: false, error: null`) is admitted, (b) a Codex/Z.ai/Claude host with valid `seven_day` (`80%`) but sibling `five_hour` carrying `error: "unavailable"` fails closed (`unknown`) through both `host_quota_snapshots` and `fleet_subscriptions`, (c) stale `subscription.observed_at` or passed unrefreshed `reset_at` fails closed in `dispatch()`, `modelCandidates()`, and `claim()`, and (d) no API parameter or manual flag can bypass quota reserve/exhaustion/cooldown gates.

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
       - Unresolved `intake_tasks` where `human_state` / obligation is `pending_approval`, `blocker`, or `followup` (`src/intake.ts:147–150`).
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
| **`M13-IG1`** | `M13a` | Medium | Extend sanitized quota schema (`src/quota.ts`, `src/fleet.ts`, `host/agent_hub_host/quota_snapshot.py`, `host/agent_hub_host/fleet.py`) to preserve `w.error` and add `collector_version`, Codex credits, OpenCode Go native spend, and canonical `(provider, account_label, profile)` binding over existing `fleet_accounts`/`fleet_routes` (no second quota store), while keeping unknown/secret key rejection fail-closed (`400`) and never fabricating dollar/token counts from percentages. |
| **`M13-IG2`** | `M13b` | Medium | Add `hub quota` CLI (`host/agent_hub_host/cli.py`), extend `GET /api/quota` for authenticated fleet-wide and filtered reads (admin + current-incarnation authorized orchestrator tokens), deduplicate shared pools strictly by canonical verified `(provider, account_label)` on `fleet_accounts` + `fleet_routes` (never colliding unqualified labels across providers/accounts), and render `fresh \| stale \| exhausted \| unavailable \| unknown` badges and native units in `public/fleet.html` + `public/fleet.js`. |
| **`M13-IG3`** | `M13c` | **High** | Enforce fail-closed quota admission in `RoutingService.dispatch()`, `modelCandidates()`, `FleetService.accountStatus()`, and `FleetService.claim()` (with routing-owner ACK): require fresh `subscription.observed_at` and `quota.observed_at` (`<= 1200 s`), `quota.status === 'ok'`, `limit_reached === false`, `stale === false`, fail closed (`unknown`) on any supported/required sibling window with `error === 'unavailable'`, treat benignly absent optional/unsupported windows (`present: false, error: null`, e.g. Codex `5h`/`monthly` when weekly is valid) as neither `0%` nor error, enforce reserves across all available applicable (`present: true`) windows (Codex 15%, Grok 5%), require `observed_at >= reset_at` before a passed `reset_at` clears an exhausted window, and forbid any manual quota override from bypassing launch safety. |
| **`M14-IG1`** | `M14a` | Medium | Upgrade `hub status` CLI (`host/agent_hub_host/cli.py`) from local-config stub to single-call `GET /api/status` (`--since`, `--json`), enforce synchronous transaction-time caller scoping (`M12-C1`), and report per-section `{source, observed_at, freshness, coverage, missing_hosts}` with graceful `partial` coverage when hosts are offline/stale. |
| **`M14-IG2`** | `M14b` | **High** | Collect every-host per-task session health (`working \| waiting \| stopped \| limit-hit \| stuck \| uncertain`, `limit_hit_kind`, `context_low`, `has_unsubmitted_draft`, `draft_byte_length`) strictly read-only (**never** sending `Enter`/keys, restarting jobs, or uploading raw draft/transcript text), and collect allowlisted task-linked report headlines/verdicts with explicit `author_claim` vs `independent_review_verdict` provenance. |
| **`M14-IG3`** | `M14c` | **High** | Compute `deployed` for landed `main` commits strictly from Git ancestry in the **currently active deployed revision** (`active_revision_sha`): rollbacks (`wrangler rollback`) must demote rolled-back commits to `not_deployed (rolled_back)`, newer failed/pending deploys must not replace the last confirmed active deployment, and CI state (`ci_state`) must remain separate from `deploy_state`. |
| **`M14-IG4`** | `M14c` / `M14d` | Medium | Join pending human actions (`intake` approvals/blockers/follow-ups, `waiting-on-human` jobs, pending `device_pairings`) with `user_code` restricted strictly to authenticated human admins (never exposing `device_code_hash` or tokens), and key `M14d` diff watermarks by `(caller_key, scope_key)` with hard row/byte caps and honest `expired \| first_observation` baseline reporting. |

---

## 8. Rollout, Fallback, and Cross-Host Runtime Acceptance Order

1. **Gate 0 — Execute in Actual Operator Intake Priority Order (`M12e` First -> `M12b/c` Second; Spare-Capacity `M13`/`M14` Contract/Source Work Without Preemption)**:
   - **Priority 1 (`M12e` live loop + `M9` host-authority dependency)**: Complete the independent Argon R2 review of `M12e` (`2ca4700a80af316538319da462a4c17dac17867a`) and prove the live cross-host loop (`Grok Bot` on Botbox -> Hub intake/task -> Hetzner native `aplexer` session -> progress/completion return), together with the shared `M9` host-authority core (`35047954f4711d96f3becfbddf5332388958431c` plus the two HTTP negative tests on `/api/oversight/actions` and `/api/fleet/launches/:id/claim`). Full `M9` accounting DTO/UI (`f20b35a60af2de17c91303dd317da70b44f56174`) is an independent track and **not** a blanket prerequisite for `M12`, `M13`, or `M14`.
   - **Priority 2 (`M12b/c` actual assignment, backfill, and fresh-agent handoff)**: Complete `M12b` (`POST /api/intake/tasks/:id/assign` -> `RoutingService.dispatch()` -> `fleet_launches`) and `M12c` (`POST /api/intake/backfill` multi-host reconciliation and `hub handoff` adoption on a fresh agent session).
   - **Spare-capacity `M13` / `M14` contract & source work**: Once primary `M12e` and `M12b/c` work is staffed, independent `M13a–c` and `M14a–d` contract and source implementation may proceed in parallel on spare worker capacity with disjoint file ownership **without** preempting primary `M12` work and **without** waiting forever for `M12` physical operations ACKs; actual multi-host runtime rollout and acceptance of `M13`/`M14` retain their real upstream dependencies.
2. **Gate 1 — `M13a -> M13b -> M13c` Rollout, Fallback & Non-Bypassable Launch Safety**:
   - Deploy/land Hub schema `1.1` additive quota ingestion (`M13a`) and `GET /api/quota` + `hub quota` (`M13b`) before enabling upgraded host collectors across `Hetzner`, `ovh2`, `mac`, `win`, and `botbox`.
   - During rollout, hosts still sending schema `1.0` snapshots remain readable for `percent_remaining` windows; providers or hosts lacking new native-unit collectors report `unknown` / `unavailable` for uncollected fields rather than failing the host check-in.
   - When `M13c` is enabled, any route with `stale`, `unavailable` (on a supported/required window), `unknown`, or `exhausted` quota fails closed for automatic routing and launch-claim admission. **No manual quota override may bypass `exhausted`, `unknown`, `stale`, reserve (`15%` Codex, `5%` Grok), or cooldown launch safety**; human operators may inspect quota state via `hub quota` / `hub status` / dashboard and hold work in `queued` / `waiting-on-human` until a healthy eligible route or post-reset snapshot is verified.
3. **Gate 2 — `M14a -> M14b -> M14c -> M14d` Rollout & Fallback**:
   - `M14a` (`GET /api/status` + `hub status`) works immediately over existing Hub tables and reports `coverage: 'partial'` for hosts that have not yet upgraded their host agent to emit `M14b` session/report observations.
   - `M14b` host session/report observers run strictly read-only with bounded timeouts; if local `aplexer` inspection or report reading fails on a host, the host reports `health_state: 'uncertain'` / `report_status: 'unavailable'` without blocking heartbeats or mutating sessions.
   - `M14c` deployment status defaults to `deploy_state: 'unknown'` until an authenticated deployment/rollback receipt is recorded, preventing false `deployed: true` claims.
4. **Gate 3 — Cross-Host Runtime Acceptance vs Unit Tests**:
   - Local `vitest` and `python3 -m unittest` suites verify `M13-IG1`..`M13-IG3` and `M14-IG1`..`M14-IG4` at source level, but **runtime acceptance** of `M13` and `M14` requires live multi-host evidence (`Hetzner` + `botbox` + enrolled fleet hosts) verified by an independent model reviewer and acknowledged by live ops custody.

---

## 9. Inspection Commands, Containment, and Resource Receipt

### 9.1 Read-Only Inspection Commands Executed (`R1` and `R2`)

1. **Original `R1` review (`platform-quota-status-plan-review` / `1f2004e9-3d3f-45f1-926e-1875058539e8`, commit `b033eeade5b6718e15c47df08ff8989f523cdfe9`)**:
   - `a whoami --json > .local/review/first-action-worker.json` and `a context`; sent task ACK `01a14968-f98b-7551-9843-2c68f0e75c65` and acknowledged coordinator reply `01a14969-669c-73e1-9939-0609baad5f13`.
   - Read `/home/alexey/tmp/agent-reports/hub-platform-brief.md` first, then `/home/alexey/tmp/agent-reports/hub-intake-brief.md` (Base + Addenda 1–4), `AGENTS.md`, reviewer role docs, `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and `STATUS.md`, and Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb` sources, writing `.local/review/inspected-hashes.json` (`28` Hub source/doc entries + `1` coordinator `PLAN.md` entry, all `28` Hub file hashes/bytes/blobs independently verified by `/home/alexey/git/agent-coordination/.local/platform/quota-status-plan-review-source-receipt-audit.json`).
2. **Independent `R2` deep correction review (`platform-quota-status-plan-review-r2` / `5de3093f-a3a3-48d8-b400-1b00e84899ff`, task `0432`)**:
   - First tool action ran `mkdir -p .local/review/meta-r2 && a whoami --json > .local/review/meta-r2/first-action-worker.json && a context`.
   - Sent own identity/task `0432` ACK (`01a1497c-68c3-7ee3-9ecf-6a88ed8f9f1d`, `.local/review/meta-r2/ack-sent.json`) to `platform` in `/home/alexey/git/agent-coordination` and acknowledged reply `01a1497d-861c-7e60-bd66-5cf35f8e4c53`.
   - Re-read `/home/alexey/tmp/agent-reports/hub-platform-brief.md` first, then full `/home/alexey/tmp/agent-reports/hub-intake-brief.md` (Base + Addenda 1–4), `AGENTS.md`, reviewer role docs, `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and `STATUS.md`, head pre-audit artifacts (`/home/alexey/git/agent-coordination/.local/platform/quota-status-plan-review-report-preaudit.json`, `quota-status-plan-review-source-receipt-audit.json`, `quota-status-plan-review-start.json`), preserved R1 `.local/review/guard-receipt.json`, Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb` source/test files (`test/quota.test.ts`, `test/fleet.test.ts`, `test/routing.test.ts`, `host/tests/test_quota_snapshot.py`, `host/tests/test_fleet.py`), and commit `b033eeade5b6718e15c47df08ff8989f523cdfe9`. No product tests, builds, or dependency installs were run.

### 9.2 Containment and Resource Receipts (`R1` Preserved Provenance + `R2` Independent Witness)

1. **`R1` (`platform-quota-status-plan-review` / `1f2004e9-3d3f-45f1-926e-1875058539e8`) Retained Provenance & Withdrawn Unproven Kernel Peaks**:
   - **Retained outer `aplexer` launch containment record** (`/home/alexey/git/agent-coordination/.local/platform/quota-status-plan-review-start.json` and `.local/review/first-action-worker.json`):
     - Outer `containment_cgroup`: `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-1f2004e9-3d3f-45f1-926e-1875058539e8.scope`
     - Outer configured limits: `memory.max = 1,572,864,000` bytes (`1500 MiB`), `pids.max = 256` tasks, `cpu.max = 100000 100000` (`1.0` CPU core). This start record proves the outer configured limits and outer containment path, not whole inner-model runtime measurements after the maintained `agy` wrapper moved the inner workload into its own scope.
   - **Withdrawn `R1` inner cgroup path & kernel watermark claims (`UNKNOWN`)**:
     - Commit `b033eeade5b6718e15c47df08ff8989f523cdfe9` reported an inner scope path (`/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/agent-platform-quota-status-plan-review-1776420735.scope`) and kernel watermarks (`memory.current = 193,167,360`, `memory.peak = 218,652,672`, `pids.current = 13`, `pids.peak = 14`), but `R1` did **not** retain a raw cgroup witness file in `.local/review/` before its transient scope exited. Those unverified `R1` inner cgroup path and kernel `memory.current` / `memory.peak` / `pids.current` / `pids.peak` figures are therefore **withdrawn as `UNKNOWN`** (and `R2`'s fresh kernel watermarks are not substituted for `R1`).
   - **Retained `R1` discrete `.local` guardian receipt (`.local/review/guard-receipt.json`)**:
     - `exit`: `0`, `preventive_abort`: `false`
     - `sampled_peak_allocated_bytes`: `643,072` bytes (`~628.0 KiB`, well below the `3,145,728` B early guard and `4,194,304` B hard cap)
     - `samples`: `3047`, `observed_max_sample_gap_seconds`: `0.26633023750036955`, `elapsed_seconds`: `763.918491018936`, `sampled_not_continuous`: `true` (explicitly discrete polling).
2. **`R2` (`platform-quota-status-plan-review-r2` / `5de3093f-a3a3-48d8-b400-1b00e84899ff`) Independent Witness (`.local/review/meta-r2/cgroup-witness.json`)**:
   - **Verified `aplexer` containment cgroup (`cat /proc/self/cgroup` and `.local/review/meta-r2/first-action-worker.json`, retained in `.local/review/meta-r2/cgroup-witness.json`)**: `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/aplexer-workload-5de3093f-a3a3-48d8-b400-1b00e84899ff.scope` (`memory.max = 1,572,864,000` B / `1500 MiB`, `pids.max = 256`, `cpu.max = 100000 100000` / `1.0` CPU core).
   - **Discrete sampled `R2` observations** (for `R2` only at `2026-10-17T11:02:28Z` in `.local/review/meta-r2/cgroup-witness.json`, not attributed to `R1`): `memory.current = 216,875,008` B (`~206.8 MiB`), `memory.peak = 245,243,904` B (`~233.9 MiB`), `pids.current = 13`, `pids.peak = 14`; combined `.local/` disk usage (including all preserved `R1` files + `.local/review/meta-r2/`) `~660 KiB` (`< 0.65 MiB`, well within the `3 MiB` early / `4 MiB` hard cap).

---

## 10. Correction Provenance (`R2` Over Immutable Parent `b033eeade5b6718e15c47df08ff8989f523cdfe9`)

This `R2` revision is authored by an independent Argon session (`platform-quota-status-plan-review-r2` / `5de3093f-a3a3-48d8-b400-1b00e84899ff`, task `0432`) as a direct child commit on sole parent `b033eeade5b6718e15c47df08ff8989f523cdfe9` (authored by `platform-quota-status-plan-review` / `1f2004e9-3d3f-45f1-926e-1875058539e8`). All original `R1` private receipts in `.local/review/` are preserved untouched; `R2` receipts live exclusively under `.local/review/meta-r2/`.

### Summary of Corrections Applied in `R2`

1. **Fixed 3 nonexistent 40-hex Git refs and 1 unverified short prefix from `b033eeade5b6718e15c47df08ff8989f523cdfe9`**:
   - Replaced the prior report's nonexistent `27e4b32`-prefixed 40-hex string (`27e4b3..db984`) with the verified Hub commits that introduced and refined `docs/reviews/INTAKE-TASKS-PLAN.md` in `6bc91a1278250b374ea1d0b11acd353fae51a8cb` history: `1f000fea476cc62ec0b98cc4764e96b6d364bbad` (`docs(review): review M12 intake assignment backfill handoff and session plan [skip ci]`), refined by `a13d227c6486221b5fb5f8b31948648cdea1f20d` (`docs: refine M12 intake plan review contracts and verify 27e4b32 [skip ci]`) and merged in `037bf039ec5ae01799a1cc863d31178a76107cdf` referencing coordinator commit `27e4b32487eb556cab624dfcc638cfa43faa0dd1`.
   - Replaced the prior report's nonexistent `2ca4700`-prefixed 40-hex string (`2ca470..49492`) with the true Hub `M12e` session R2 repair commit `2ca4700a80af316538319da462a4c17dac17867a` (`[skip ci] M12e repair: reconcile delayed replies, qualify senders, enforce live family, durable acks, canonical Fleet binding, bounded pagination`).
   - Replaced the prior report's nonexistent `3504795`-prefixed 40-hex string (`350479..a0f25`) with the true Hub `M9` core host-authority commit `35047954f4711d96f3becfbddf5332388958431c` (`fix(enrollment): stable server-owned approval authority for host families [skip ci]`), and referenced verified `M9` metrics commit `f20b35a60af2de17c91303dd317da70b44f56174` (`docs(metrics): unbound issuance requires the current witness when one exists [skip ci]`).
   - Replaced the prior report's unverified short `477bd..23` ref with verified Hub `M12a` commits `adae0fb346028c449db7ce6b5858720523ffd6a9` / `c290454e88c49c6fe863d20dd2717d58522851d0` / `bc796013d9f522d8a2e3ea050f7b264e2fabfc41`.
   - Recorded the exact subject of Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb` (`docs: correct independent review Git metadata before publication [skip ci]`).
2. **Corrected Milestone Map Labels & Cloudflare Free-Plan Bound Claim**:
   - Corrected `M10` (`Real Host Operations & Dogfooding`) and `M11` (`Operator Alerting & Active Remediation`) to match `docs/SPEC.md` and `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` (role leases and retention belong to `M3` / `src/schema.ts`, not `M10`/`M11`).
   - Removed the overclaim that application code enforces a hard Cloudflare account billing cap, stating accurately that application code enforces payload/row/retention/alarm discipline while account-level Cloudflare billing caps are external platform controls (`AGENTS.md` USD 5/month Free-plan constraint).
3. **Corrected Resource & Cgroup Provenance (`Section 9.2`)**:
   - Withdrew `R1`'s unverified inner cgroup path (`agent-platform-quota-status-plan-review-1776420735.scope`) and kernel `memory.current` / `memory.peak` / `pids.current` / `pids.peak` figures as **`UNKNOWN`**, retaining `R1`'s verified outer `aplexer` launch record (`/home/alexey/git/agent-coordination/.local/platform/quota-status-plan-review-start.json`) and genuine discrete guardian receipt (`.local/review/guard-receipt.json`: `exit: 0`, `sampled_peak_allocated_bytes: 643,072`, `3047` samples, `max_gap: 0.26633023750036955 s`, `elapsed: 763.918491018936 s`), and recording `R2`'s own cgroup witness separately in `.local/review/meta-r2/cgroup-witness.json`.
4. **Aligned Substantive Ordering, Non-Bypassable Launch Safety, Optional Window Semantics, and Canonical Shared-Pool Deduplication (`Sections 4.1, 5, 7, 8`)**:
   - **Ordering (`Section 4.1`, `Section 8 Gate 0`)**: Restored the exact operator intake priority (`M12e` real cross-host loop first, then `M12b/c` actual assignment, backfill, and fresh-agent `hub handoff`); scoped the `M9` dependency strictly to `M9` host-authority (`35047954f4711d96f3becfbddf5332388958431c` + two HTTP negative repairs) rather than all of `M9` accounting DTO/UI; and clarified that spare-capacity `M13`/`M14` contract/source work may proceed in parallel without preempting primary `M12` work or waiting forever for `M12` physical ops ACK.
   - **No manual quota override bypass (`Section 5 F-M13-3`, `Section 7`, `Section 8 Gate 1`)**: Clarified that no manual quota override may ever bypass `exhausted`, `unknown`, `stale`, reserve (`15%` Codex, `5%` Grok), or cooldown launch safety; only read-only human inspection and holding in `queued` / `waiting-on-human` are allowed.
   - **Benign optional-window absence vs genuine sibling `error: "unavailable"` (`Section 5 F-M13-3 / M13-IG3`)**: Distinguished benign absence of optional/unsupported windows (`present: false, percent_remaining: null, reset_at: null, error: null`, e.g. Codex `5h`/`monthly` when `weekly` is valid) from genuine `error: "unavailable"` on a supported/required sibling window (which `fleet.py:139–140` and `src/fleet.ts:30–35` must preserve and which must fail closed).
   - **Canonical shared-pool deduplication (`Section 5 F-M13-1 / F-M13-2`, `Section 7`)**: Grounded deduplication in the existing `fleet_accounts` `(provider, label)` and `fleet_routes (machine_id, engine, account, profile)` contract without creating a second quota store or colliding arbitrary unqualified labels across providers or accounts.
   - **Verdict verification**: After re-auditing every operator requirement against `6750e34bc2fdff07c84e0127c973ac46210f9e5d:docs/platform/PLAN.md` and Hub `6bc91a1278250b374ea1d0b11acd353fae51a8cb`, the **`SCOPED ACCEPT`** verdict and the seven binding gate IDs (`M13-IG1`..`M13-IG3`, `M14-IG1`..`M14-IG4`) remain well-founded and are preserved with tightened specifications.

