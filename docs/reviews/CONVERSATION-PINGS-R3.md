# M5 conversation pings r3 review

**SCOPED ACCEPT — exact retirement source `212560b6331f6347519f862b93c2a5cd7975871b`.**

Independent reviewer: `platform-pings-r3-review` / `grok-4.6` (session `2f2f13cb-ba57-4a92-a260-322c5c50ef95`). Author of this SHA: `platform-pings-r3` / `GPT-6` (`2554424e` identity, distinct from this reviewer). Original rejected proposal `1229be7`, original REVISE `1fe94af`, repaired source `117f240`, and R2 report `6fb7b21` remain immutable history. This reviews the bounded lifecycle fix on exact `212560b`, not an easier partial criterion.

Source acceptance of this SHA does not integrate to main, does not enable checks, and does not close physical M5, M8b, issue 102, or M11. Full roadmap M1–M11 stays active. Head owns Agent Branches fork/WIP, integration and recovery.

Review: approved sha=212560b6331f6347519f862b93c2a5cd7975871b reviewer=platform-pings-r3-review/grok-4.6

## Verdict

The R2 MEDIUM operational-lifecycle gap is falsified on this exact source. Repeated current intervals proceed after more than 4,096 settled historical receipts are reclaimed, inside bounded SQL rows and frozen UTF-8 payloads. Unresolved intents survive cleanup, restart, injected SQL failure, overlap and the 300s boundary, and converge once. After both the message and its receipt are gone, the old key cannot authorize a new write. The current check name remains usable; there is no global HOLD at 4,096. Unknown mismatched receipts HOLD rather than deleting pending work to free capacity.

The original five findings remain repaired. Positive current use works (including a 4,000-character CJK template). One residual **LOW** defense-in-depth gap is documented below and is not a return of immortal receipts.

## Exact source under review

| Object | Full SHA | Role |
| --- | --- | --- |
| Reviewed source | `212560b6331f6347519f862b93c2a5cd7975871b` | Byte bounds + operational retirement verification |
| Direct parent | `48b310679ca472b0913f0dc8ced9ee3d1bb99d4b` | Current-slot fence, random generation, retention reclaim |
| R2 review / base | `6fb7b210b8a46b30dc27abda7839631a82877b48` | Scoped accept of `117f240` with MEDIUM HOLD gap |
| Repaired M5 | `117f24045ec402ccbd1791c2815f4f24e5bd1d7e` | Five findings repaired; receipts immortal |
| Accepted backend / merge-base | `16d66d69708268ab84089447534f0439e0bfd893` | Hub main at candidate construction |
| origin/main at this review | `10c65ffe38099d85f4ffc530dabc197e8521dde2` | Moved; merge-base with 212 remains `16d66d6` |

`212560b` parent is `48b3106`. `48b3106` parent is `6fb7b21`. Lineage `16 → 117 → 6fb → 48 → 212` is intact. Head owns any fresh-origin/main fast-forward. This reviewer did not merge, push, or restore Agent Branches.

`git diff --check` on the 6fb→212 owned source/test/docs delta is clean. `host/` is byte-identical to `16d66d6`. `test/conversation-pings.test.ts` and `src/hub.ts` SHA-256 match the R2 report. Old reports `docs/reviews/CONVERSATION-PINGS.md` and `CONVERSATION-PINGS-R2.md` are unchanged.

### Owned delta SHA-256 at `212560b`

| Path | `6fb7b21` SHA-256 | `212560b` SHA-256 |
| --- | --- | --- |
| `docs/CONVERSATION-PINGS.md` | (R2 contract) | `f958016cec924371d291582db6e7b7ec30b068b543883dc118952fe09b00ebd2` |
| `docs/GENERIC-HUB.md` | (R2 sentence) | `fd72b39a8730c814baf0f986b85d2bfbb9cf73fdb43ee822952a680ef05b68ff` |
| `src/checks.ts` | (R2 + activation-ms key) | `1f348199d7f6d42b8d0742df6533fbc860a23f73b33ec07f3b4e7ad8448fc8f8` |
| `src/conversation-automation.ts` | (R2 schema) | `08acb189d7622e3924081a48ae40db3b625b253b2d19b88334b3954c6631b03e` |
| `src/conversation-hub.ts` | (R2 wrap) | `c90aaf6ad084acd6fa92adbe5ae15c6551625c08f8975770f4fbc46556adebf6` |
| `src/conversations.ts` | (R2 writer) | `d132b3342f8c3d1864e76d4bd5bf9a0468749bb5c0538b1630432c56df844ddc` |
| `test/checks.test.ts` | (12 cases) | `48bc06b118d508e7714dfa75340338e2ff11961967038d02766cc289086cacdd` |
| `test/conversation-pings-retirement.test.ts` | absent | `3b314b31ca3a4af3a07d13053ba6e7a2fa6daf8a706471817a8ce4e2542d534b` |
| `test/conversation-retention.test.ts` | (19 cases) | `e820eb3731b82228caebbce03329de12b83d474573414340e071e98e56b69176` |
| `test/conversation-pings.test.ts` | `6d630d5bd98f1ea608c4049945957a9d501016c02771b4439259b2aab8a924b4` | equal |
| `src/hub.ts` | `02252fbed195cdccac24810b3d0848d35e6b1ea9682aa22d6bf861aa64807f9b` | equal |

Author evidence hashes for the nine changed paths match this tree. 48→212 adds the 128/64/64 KiB UTF-8 caps, minimal frozen check fields, standalone `hub_checks` guard, docs, two byte-bound cases, 21 lifecycle cases and one retention accounting case.

## Old five findings

| Original (1fe94af) | This source | Independent evidence |
| --- | --- | --- |
| HIGH: follow-up authored as recipient, watermark 0→2, native self-suppressed | **still FALSIFIED** | Probe watermarks unchanged; target `last_read_seq` 0; unread 2; captured history sender `system:conversation-checks` / `sender_kind:system` / `kind:note`. Memory bridge 2 deliveries, self-sender control 1. |
| HIGH: configured actor treated as authority | **still FALSIFIED** | Agent token 403; CSRF-less owner 403; demoted owner 403. |
| MEDIUM: lost response + detail drift | **still FALSIFIED** | Unresolved retry across the 300s boundary reuses `slot_key` and frozen intent; one message. |
| MEDIUM: overlapping runDue later-row duplicate | **still FALSIFIED** | Original 45 follow-up cases + delayed-send/cleanup race in lifecycle tests. |
| MEDIUM: typecheck fail | **still FALSIFIED** | Independent `tsc --noEmit` PASS. |

Credential rebind between evaluation and effect still fails closed. Config-time CID mismatch is still 400. Unfenced `sendMessage` as `system:conversation-checks` still throws and writes nothing.

## R2 remaining finding

### MEDIUM: receipt/grant/slot growth has no operational retirement — **FALSIFIED** on `212560b`

**Location.** `src/conversations.ts` `postScheduledFollowUp` (current non-done slot + frozen `intent_json` required; missing receipt never authorizes a write); `retentionCleanup` / `automationRetirementCandidates` (slots, receipts, grants as fair categories inside the shared row budget); `src/checks.ts` `runDue` (opaque `crypto.randomUUID()` conversation generation; `now < next_at` replay of a done slot).

**Independent proof, not copied seed counts.** Lifecycle fixture seeds 4,095 historical receipts with `json_set` of a real settled `request_json` plus that real receipt, then reclaims 4,096 rows. This reviewer re-ran that case and separately parsed eight seeded rows: each `request_json` is valid `FollowUpIntent` JSON whose `idempotencyKey` equals `effect_key` and whose `conversationId` is the live CID. After reclaim, 32 (author fixture) / 1 extra (this probe) current intervals write inside one latest slot and one grant. Member watermarks stay at the pre-reclaim snapshot. Forced backward clock on a done slot sets `replay: true` and inserts zero messages. Deleting the follow-up message and its receipt leaves the original unread row; the old intent throws `automation_slot_retired`; the next interval writes a different idempotency key. Archived-parent cleanup removes grants and receipts; replay cannot write. An inconsistent receipt (`request_json` tampered) keeps the unresolved slot and the receipt (`automationRowsDeleted=0`).

**Current-use, not always HOLD.** After reclaim, `runDue` still returns `conversation_message …`. A 4,000-character CJK template stores a real follow-up. Name reuse onto a new CID/recipient writes a new key and rejects both the old intent and a patched old intent.

## Residual finding

### LOW (non-blocking): hooked huge UTF-8 fail-detail can freeze a retrying intent that the writer rejects

**Location.** `src/checks.ts` `runDue` pre-intent gate is `intent.body.length > 8000` (JS UTF-16 units) or `jsonBytes(intent) > 64KiB`. `src/conversations.ts` `writeMessage` still enforces `MAX_MESSAGE_BODY_BYTES = 16384` UTF-8.

**Repro.** Independent probe: grant/template `{detail}`, hook `evaluateBacklog` to `{status:'fail', detail: '文'.repeat(6000)}` (JS length 6000 ≤ 8000, UTF-8 18000 > 16384). Result: `status=error`, `action=action failed: body_too_large`, `state=retry`, `intent_json` length 6254, conversation message count unchanged at 2. No new write.

**Why this does not reopen the MEDIUM gap.** Production `conversation_backlog` / `routine` details are short English strings. Templates are capped at 4,000 characters; 4,000 CJK = 12,000 UTF-8 bytes, under the writer cap, and that positive path wrote. Oversized *character* render is rejected before intent (`followup_intent_too_large`, `intent_json` null) in the author’s 21st lifecycle case, independently re-run. Unknown unsafe effects still HOLD.

**Guarded remedy (later, not required for this SHA).** Compare `TextEncoder.encode(intent.body).byteLength` to `MAX_MESSAGE_BODY_BYTES` before persisting `intent_json`.

## Independent commands and counts

Reviewer isolation: `taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, umask 077, private `TMPDIR`/`HOME`/`XDG` under `.local/`, existing `node_modules` symlink only to the hub install (untracked, not staged). Vitest `--configLoader runner`. Combined scratch cap 30MiB with 22MiB early guard. No installs, deploys, native CLI, journals, provider jobs, digest re-pins or source edits.

Final focused group is **80 = 57 original + 2 byte-bound + 21 lifecycle**. Affected compatibility unique is **53 = 26 conversations/security + 20 retention + 5 integration + 2 hub**. Author’s `compatibility-r1` line listed 52; the additive retention case makes unique 53, which this reviewer counted. Do not add 80+53 into a misleading aggregate. R2’s unrelated 206-group was not repeated.

| Command | Result | Whose |
| --- | --- | --- |
| `tsc --noEmit` | PASS, 1.244s | this reviewer (`typecheck-r1`) |
| `vitest … test/conversation-pings.test.ts test/checks.test.ts` | 59/59 PASS | this reviewer (`focused-r1`) |
| `vitest … test/conversation-pings-retirement.test.ts` | 21/21 PASS | this reviewer (`retirement-r1`) |
| `vitest …` pings+checks+retirement | 80/80 PASS | this reviewer (`final-focus-r1`) |
| conversations + security + retention + integration + hub | 53/53 PASS | this reviewer (`compat-r1`) |
| `.local/review/probes/m5-r3-adversarial.test.ts` | r1–r3: probe assertion mistakes, logs retained; r4: 12/12 PASS | this reviewer |
| memory-only `ConversationBridge.cycle` on captured history | 4/4 PASS | this reviewer (`bridge-r1`) |

Probe r1–r3 failures were probe bugs, not source defects: `on=fail` after wiping unread history returns a null action; a done-slot forced rerun *replays* the stored `conversation_message` action without inserting rows. Logs were not deleted or relabelled. r4 asserts replay inserts zero messages, then a later interval with the original unread still present writes a new key.

Adversarial r4 coverage: agent cannot grant; owner demotion 403; credential rebind fails closed; old grant cannot write a new CID; every member watermark unchanged; unresolved retry across 300s reuses `slot_key`; archived parent retires receipts; unfenced system send throws; seeded receipt JSON inspected; old key after message+receipt delete cannot write while the next interval can; backward clock does not mint a generation; inconsistent receipt HOLDs; 4,000 CJK template writes.

### Memory-native bridge (not physical delivery)

Captured Worker history (`probes-r4` snapshot SHA-256 `d35ef49d12b11899511643fa954cf5edf05d894040a07d8ad3153e60575f0406`): `my_read_seq=0`, `unread_count=2`, senders `human:pings-owner` then `system:conversation-checks` / `sender_kind:system` / `kind:note`. Idempotency key `check-followup-42b67e39-1992-4e72-8fcb-322fffb436bf` (UUID generation, not wall-clock).

Actual `host/agent_hub_host/conversation_bridge.py` `cycle` with journal/member/native/save replaced by memory doubles:

- actual follow-up: 2 `deliver` calls, cursor 2
- recipient self-sender control: 1 `deliver` (self suppressed)
- distinct human control: 2 `deliver`
- interior gap: `BridgeCapabilityGap`, cursor stays 0, no delivery

Bridge contract file SHA-256 `385dfae94c8a70db3b0fd14b8eb2c6191197e9ba55c7ae02c631f8ef420adf62` (byte-identical to the R2 contract). This is source contract evidence. No `--native`, no journal files, no production ping, no enrollment.

## Auth, scheduler, durability inspection (source)

- New conversation slot keys are `name-${crypto.randomUUID()}` when `action.conversation_id` is set; non-conversation checks still use interval number `n`. Existing R2 rows are not rewritten on `init`.
- `postScheduledFollowUp` returns a committed receipt as before; without a receipt it requires `hub_check_slots` row matching name, `check-`+`slot_key`, exact `intent_json` and `state != 'done'`.
- Retention categories: invalid-grant / removed-check slots (close, delete only if orphan), receipts whose slot is not non-done, expired/revoked/parentless grants unreferenced by a non-done slot. Fetches use `LIMIT` of the remaining budget. Table caps still bound anti-joins. Slot closure that is an UPDATE is not counted as a deletion (`automationRowsDeleted` tracks actual deletes). Inconsistent receipt/intent pairs are skipped.
- Byte caps: `validateCheck` / new-slot freeze 128 KiB; result 64 KiB (`check_result_too_large` without storing the huge blob); intent 64 KiB plus 8,000-character render, before `intent_json` is stored. Frozen check JSON is `{name,kind,interval_s,params,action}` without duplicating SQL status columns. Writer 16 KiB UTF-8 and original body/char caps remain.
- `DEFAULT_CHECKS` remains liveness-only. No new timer, provider dispatch, or PTY path.
- Standalone `ConversationsService.init()` still creates the slot schema; missing `hub_checks` does not abort grant-side retirement.

## Resources

Assigned combined cap 30MiB with 22MiB early guard on `.local/review` + `.local/tmp` (directory inodes included). No guard fired. No evidence deleted. Author `.local/pings-r3/final-evidence.json` was read-only for commands/hashes; private config/task/token files were not read.

| Peak | This reviewer | Author (retained, not this process) |
| --- | --- | --- |
| Scratch allocated | 13,422,592 (`compat-r1`) | 13,344,768 |
| Scratch apparent | 13,340,022 | 13,255,397 |
| Process-group RSS | 652,730,368 | 638,775,296 |
| Tasks | 28 | 28 |

Session cgroup: `memory.max` 1,572,864,000; `pids.max` 256; observed `memory.peak` 654,991,360. Untracked `node_modules` symlink to `/home/alexey/git/agent-hub/node_modules` was required for tests and was not staged.

Owner-relative evidence (gitignored): `.local/review/{run.py,vitest.config.ts,pins.json,bridge-snapshot.json,probes/*,*-r1.json,*-r1.log,probes-r2.*,probes-r3.*,probes-r4.*,final-focus-r1.*,compat-r1.*,bridge-r1.*,typecheck-r1.*,focused-r1.*,retirement-r1.*}`. Probe SHA-256: adversarial `d3d725ad4c43b7b8b450860f567bce52fc4b837a2ac994a43772988d340be33d`.

## Acceptance disposition

| Criterion | Verdict |
| --- | --- |
| Exact SHA / lineage 16/117/6fb/48/212 | PASS |
| Five original findings remain repaired | PASS |
| Repeated intervals after >4096 historical receipts, bounded rows/bytes | PASS |
| Unresolved pre/after/accepted intents survive cleanup/restart/SQL abort/overlap/300s | PASS |
| Retired key after message+receipt delete cannot write; current interval usable | PASS |
| Grant/name/CID/recipient/config reuse cannot revive old intent | PASS |
| Random generation does not bypass `next_at` on backward clock | PASS |
| Retention counts actual SQL deletes in the shared fair limit; preview/accounting preserved | PASS |
| Oversize character render rejected before intent; writer caps intact | PASS |
| Positive current use (not always HOLD) | PASS |
| Typecheck | PASS |
| Memory-native eligibility | PASS (memory-native only) |
| Hooked huge UTF-8 detail vs 16 KiB writer | LOW residual; fail-closed, non-blocking |
| Source integration / Agent Branches / main restore | Head-owned, not performed |
| Physical M5 / issue 102 / M8b / M11 | UNPROVEN / OPEN |

Work declaration released. Owned path `docs/reviews/CONVERSATION-PINGS-R3.md` only. Ignored evidence retained for head checks. Peers and other worktrees preserved.
