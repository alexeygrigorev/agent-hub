# M5 conversation pings r2 review

**SCOPED ACCEPT — exact repaired source `117f24045ec402ccbd1791c2815f4f24e5bd1d7e`.**

Independent reviewer: `platform-pings-r2-review` / `grok-4.6` (session `cfcf0df0-568a-4c98-a6b1-34c79dbf8b7e`). Author of this SHA: `platform-pings-r2` / `GPT-6` (`0a4a2fb9` identity, distinct from this reviewer). Original rejected proposal `1229be75432dc55f9939135d1a14a7e6e6e6a04b` and review `1fe94af` remain immutable history. This is a new exact-source review, not a review of an inert patch.

Source acceptance of this SHA does not integrate to main, does not enable checks, and does not close physical M5, M8b, issue 102, or M11. Full roadmap M4/M5/M7/M8a/M8b/M9/M10/M11 stays active.

## Verdict

The five original REVISE findings are falsified on this exact source. Follow-ups use reserved internal sender `system:conversation-checks`, leave every member read cursor unchanged, remain eligible for the existing bridge self-filter, require an expiring human-owner grant with CSRF, and bind durable slot intent plus a Hub receipt before retries.

One remaining **MEDIUM** operational-lifecycle gap is documented by the author and independently confirmed: Hub receipts never retire, so 4,096 successful effects then fail closed forever; expired/revoked/orphaned grants still occupy the 1,000-row cap. Safe HOLD is the correct duplicate-prevention choice. It is not operational retirement. It does not reopen the five source defects. Production enablement still needs an independently reviewed retirement path. Physical M5 remains UNPROVEN.

Review: approved sha=117f24045ec402ccbd1791c2815f4f24e5bd1d7e reviewer=platform-pings-r2-review/grok-4.6

## Exact source under review

| Object | Full SHA | Role |
| --- | --- | --- |
| Reviewed source | `117f24045ec402ccbd1791c2815f4f24e5bd1d7e` | Final docs + lease fixture pin |
| Direct parent | `d4affc7ad0da9c2989f81a780c657a05bd43405e` | Merge of c468 source onto accepted 16 |
| Implementation checkpoint | `c468635d9994fd1246e12b321d5206560164f92c` | Granted follow-ups and durable slots |
| Accepted backend / merge-base | `16d66d69708268ab84089447534f0439e0bfd893` | Current hub main |
| Rejected original M5 proposal | `1229be75432dc55f9939135d1a14a7e6e6e6a04b` | Immutable |
| Original REVISE | `1fe94af` blob SHA-256 `2281c407a505c221359594563130f4bd3ecc527d4fe4c4ebbf756101dd478343` | Immutable |

`d4affc7` parents are `c468635` and `16d66d6`. `117f240` vs `d4affc7` changes only `docs/CONVERSATION-PINGS.md` and `test/conversation-pings.test.ts` (lease fixtures pinned to slot+60s). All TypeScript implementation bytes at `117f240` equal `c468635`.

Changed vs `16d66d6` (12 paths). All other tracked paths match that accepted backend. Independently confirmed: 10 incoming host paths + 3 Windows docs are byte-identical to `16d66d6`.

### Owned delta SHA-256 at `117f240`

| Path | `16d66d6` SHA-256 | `117f240` SHA-256 | vs c468 |
| --- | --- | --- | --- |
| `docs/CONVERSATION-PINGS.md` | absent | `b2dd13a95603bed1b374ff62d4f0d7564a96b6a81ee7066ccf72c50474940e34` | docs only |
| `docs/GENERIC-HUB.md` | `67c5e01c44591a53bc328a228480542cadfc7559bdae08e420017085a3023331` | `ebde1d13250b85a801b227e1ee5129634f8041fe5166e3a13fee979aedef5f1f` | equal |
| `docs/proposals/CONVERSATION-PINGS.md` | absent | `6f08d9f07913cb02b852ed8626a8e3683c0ab06e636b32250444dfc0f207eb41` | equal (rejected proposal preserved) |
| `docs/proposals/CONVERSATION-PINGS.patch` | absent | `9d9c16582c6837a24f6ab771061df71803fcb35fb50b4822df8d3be403ac5f6c` | equal (rejected patch preserved) |
| `docs/reviews/CONVERSATION-PINGS.md` | absent | `2281c407a505c221359594563130f4bd3ecc527d4fe4c4ebbf756101dd478343` | equal (original REVISE preserved) |
| `src/checks.ts` | `4146fae3cc1695e2cb32bc7916d0f4aaef3f4649ea3184cc194378a20f5beb67` | `8d765dda0fa08f48de1c480182fa864faaa7425c1a4580bc40d7f116deb327ae` | equal |
| `src/conversation-automation.ts` | absent | `d6438799342ab1170335fb353c0f28bb4dbb3551d84a75eaff6da82ba14c6d7a` | equal |
| `src/conversation-hub.ts` | `948457385592374ab455523a5727614fcff1c12661c954b707e011c8cf0e0b72` | `4bc5a90fa6b8fe0c863524193ae2a913d9908a81a4c997108d84fe1f1adfba60` | equal |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | `b7e976e8674d3f6a70d7da5e8709ea559e747538bf4f32bca891aaf7e38124f5` | equal |
| `src/hub.ts` | `d0444b58d216bf434798e5fd466550aa1b8c7cb5fe96700dddd57c75c2020ce7` | `02252fbed195cdccac24810b3d0848d35e6b1ea9682aa22d6bf861aa64807f9b` | equal |
| `test/checks.test.ts` | `5529b290755ecf05ed830bc96350c920a8d379a8a7f20060128bf937c3c8eea2` | `cd9463b568156ff13f8de83e552523cdc44def15edec2dfcf431bc5a981a8350` | equal |
| `test/conversation-pings.test.ts` | absent | `6d630d5bd98f1ea608c4049945957a9d501016c02771b4439259b2aab8a924b4` | fixture clock pin only |

`git diff --check` on owned source/test/final-docs is clean. The inherited rejected patch still has trailing-whitespace warnings; that is not a new code finding and was not edited.

## Old five findings

| Original (1fe94af) | This source | Independent evidence |
| --- | --- | --- |
| HIGH: follow-up authored as recipient, watermark 0→2, native self-suppressed | **FALSIFIED** | Shared writer still advances only `principal.actor`. Automation principal is `system:conversation-checks`, not a member, so the UPDATE matches zero member rows. Target `last_read_seq` stays 0; unread_count 2. Memory-native bridge delivers the system note (2 deliveries) and still suppresses a recipient-authored control (1 delivery). |
| HIGH: configured actor treated as authority; disabled human / bare alias / outsider admin | **FALSIFIED** | Grant requires current session, CSRF, conversation owner, and `users.is_admin=1`. Agent/host tokens and demoted owners get 403. Bare `recipient` rejected at grant and at config. Outsider admin cannot GET or grant. Credential/membership/owner disable/revoke/expire fail closed before and after evaluation. |
| MEDIUM: lost response + detail drift → idempotency_conflict, no recovered receipt | **FALSIFIED** | Slot freezes intent/body/key. Replay after injected loss, read-cursor change, config replace, and ChecksService restart returns the first message id. Status is `error` while unresolved, never healthy/delivered. |
| MEDIUM: overlapping runDue later-row duplicate | **FALSIFIED** | Names are re-read after each await; a 30s lease + CAS token fences stale evaluators. Overlap probe: later conversation check evaluates once and sends once. |
| MEDIUM: typecheck fail in new fixture | **FALSIFIED** | Independent `tsc --noEmit` PASS on this SHA. |

## Remaining finding

### MEDIUM: receipt/grant/slot growth has no operational retirement; HOLD at cap is not usable M5 over repeated intervals

**Location.** `src/conversations.ts` `postScheduledFollowUp` (count ≥ 4096 → `automation_capacity`); `configureAutomation` (1,000 grants); `src/checks.ts` `runDue` (1,000 slots); `retentionCleanup` deletes conversations/members/messages only; `docs/CONVERSATION-PINGS.md` already names this as an operations limitation.

**Repro.** Independent probe archived a conversation and ran retention: the Hub receipt and grant rows remained. Author fixture filling 4,096 receipts then running a due check returns `error` / `automation_capacity` without touching unread history. Conversation slot keys include activation milliseconds, so every successful interval inserts a new immortal receipt. One 300s check reaches the cap in 4,096 × 300s ≈ 14.2 days, then every later interval HOLDs. Expired, revoked, and parent-pruned grants still count toward 1,000.

**Why this is not a fifth-finding regression.** Silent prune of a live effect key would permit duplicates. Fail-closed HOLD is the safe default. Unresolved retry across a 300s boundary reused the original `slot_key` and wrote one message.

**Guarded remedy (head / later implementer).** Independently reviewed retirement that (1) never deletes a receipt whose `effect_key` is referenced by a non-done slot intent, (2) may drop receipts only after that slot is `done` and a newer slot_key exists, (3) may drop revoked/expired grants only when no unresolved slot references `grant_id`, (4) keeps HOLD if retirement is unset, (5) does not weaken source assertions or re-pin digests. Safe HOLD alone must not be treated as M5 operational completion.

**LOW (non-blocking).** Member-removal trigger writes `revoked_at = 1` while HTTP revoke writes `now()`; both are non-null so both fail closed. `writeMessage` still issues a sender watermark UPDATE; for the reserved non-member sender it is a no-op.

## Independent commands and counts

Reviewer isolation: `taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, umask 077, private `TMPDIR` / HOME / XDG under `.local/`, existing `node_modules` symlink only (untracked, not staged). No installs, deploys, native CLI, journals, or provider jobs.

| Command | Result | Whose |
| --- | --- | --- |
| `tsc --noEmit` | PASS, 1.359s | this reviewer (`typecheck-r1`) |
| `vitest run test/conversation-pings.test.ts test/checks.test.ts` | 57/57 PASS (45 follow-up + 12 checks) | this reviewer (`focused-r1`) |
| verbose watermark case for HTTP snapshot | 1 PASS, captured public history | this reviewer (`snapshot-r1`) |
| `.local/review/probes/m5-adversarial.test.ts` | r1: 2 FAIL / 6 PASS (fixture assertions; logs retained); r2: 8/8 PASS | this reviewer |
| memory-only `ConversationBridge.cycle` on captured history | 4/4 PASS | this reviewer (`bridge-r1`) |
| `test/conversations.test.ts` + `test/conversation-security.test.ts` | 26/26 PASS | this reviewer (`conv-security-r1`); writeMessage/schema reason |
| `test/conversation-retention.test.ts` + `test/conversation-integration.test.ts` | 24/24 PASS | this reviewer (`retention-r1`); retention vs new tables |
| `test/hub.test.ts` | 2/2 PASS | this reviewer (`hub-r1`); extraCheckDeps wiring |
| Author 206 compat (107+26+24+35+14) | attributed after hash-check of those test files vs `16d66d6` (all identical); 26+24 rerun above; remaining 107 not repeated (no additional code-finding reason inside 35MiB) | author, with this reviewer's file-hash attribution |
| Author memory bridge 4/4 | matches this reviewer's 4/4 on a fresh snapshot | author + this reviewer |

`probes-r1` failures were probe bugs, not source defects: config already returns 400 for params/action CID mismatch (stronger than the probe expected), and `every last_read_seq === 0` ignored the owner's ordinary self-send cursor. Logs were not deleted or relabelled. r2 asserts config-time 400, runtime grant/CID mismatch, and target watermark 0 with all member rows unchanged across the ping.

Adversarial r2 coverage: agent/host cannot issue grants; owner demotion 403; credential id rebind between eval and effect fails closed; old grant cannot write a new CID; omitted params CID is not a wildcard write; every member watermark unchanged; unresolved retry across the 300s boundary reuses `slot_key`; archive+prune leaves receipts; unfenced `sendMessage` as `system:conversation-checks` throws and writes nothing.

### Memory-native bridge (not physical delivery)

Captured Worker history (`snapshot-r1`, SHA-256 `b89cbece94c8bd2fe838ac45660d514064a6d6b542828cd65f99e4efdddfe2cb`): `my_read_seq=0`, `unread_count=2`, senders `human:pings-owner` then `system:conversation-checks` / `sender_kind:system` / `kind:note`.

Actual `host/agent_hub_host/conversation_bridge.py` `cycle` with journal/member/native/save replaced by memory doubles:

- actual follow-up: 2 `deliver` calls, cursor 2
- recipient self-sender control: 1 `deliver` (self suppressed)
- distinct human control: 2 `deliver`
- interior gap: `BridgeCapabilityGap`, cursor stays 0, no delivery

This is source contract evidence. No `--native`, no journal files, no production ping, no enrollment.

### Cross-interval note

Author pinned both lease fixtures to `floor(now/300000)*300000+60000` after a real clock crossed a slot boundary. That pin was not accepted as proof. Independent probe started an unresolved retry 1s before `next_at`, advanced 5s past the boundary, and observed the same `slot_key` / frozen intent / single message. A later due run may start a new interval after `done`; that is a second generation, not a duplicate of the unresolved slot.

## Auth, scheduler, durability inspection (source)

- Grant API: `PUT`/`DELETE /api/conversations/:cid/automation` after session+CSRF in `handle`, then `configureAutomation` requires `auth.session`, `authType==='session'`, `csrfVerified`, owner membership, human kind, live `users.is_admin`. CallerContext alone is refused.
- Target is exact canonical member; agent credential is pinned at issuance and revalidated with `verifyPrincipal` on that credential id (revocation, expiry, kind, registry actor, machine, read scope). Rotated credentials need a new grant.
- Reserved sender cannot be device-registered (`system` namespace). JSON sender override fields remain forbidden. No public trusted caller for automation writes.
- `evaluateScheduledBacklog` requires explicit grants, caps 50, errors rather than false healthy, excludes self-sends, uses `seq > last_read_seq` and `created_at` age. Optional params CID is not a write wildcard; action still needs matching grant/CID/template/`on`.
- Slots: latest row per check name, 30s lease, CAS token, frozen `check_json`/`intent_json`/`result_json`. Conversation `slot_key` is `name-activationMs`. Retry keeps original intent across interval changes and across the 300s boundary until `done`.
- Message + receipt insert share the Hub reentrant `transactionSync`. Receipt replay after revoke/prune returns the original id and performs no new write. New effects still require current grant.
- `DEFAULT_CHECKS` remains liveness-only. No new timer, provider dispatch, or PTY path.
- Ordinary F7/M6/retention/body limits unchanged on the paths inspected; 26+24+2 inherited tests passed on this SHA.

## Resources

Assigned combined cap 35MiB with 24MiB early guard on `.local/review` + `.local/tmp` (directory inodes included). No guard fired. No evidence deleted.

| Peak | This reviewer | Author (retained, not this process) |
| --- | --- | --- |
| Scratch allocated | 13,807,616 (`conv-security-r1`) | 33,796,096 with 8KiB early-inode caveat |
| Scratch apparent | 13,693,546 | 33,663,424 |
| Process-group RSS | 601,784,320 | 651,788,288 |
| Tasks | 30 | 30 |

Session cgroup at last reading: `memory.max` 1,572,864,000; `pids.max` 256; `memory.peak` 597,225,472; `pids.current` 73. Final combined scratch after runs: 217,088 allocated / 124,029 apparent. Untracked `node_modules` symlink to the existing hub install was required for tests and was not staged.

Owner-relative evidence (gitignored): `.local/review/{run.py,vitest.config.ts,pins.json,bridge-snapshot.json,probes/*,*-r1.json,*-r1.log,probes-r2.json,probes-r2.log}`. Probe SHA-256: adversarial `d02b97001c82c3cd93bde06249df3650cae9c34044c20e69bd1847d406e629e6`; bridge contract `385dfae94c8a70db3b0fd14b8eb2c6191197e9ba55c7ae02c631f8ef420adf62`. Author `.local/pings-r2/final-evidence.json` was read-only for counts/caveats; private config/task/token files were not read.

## Acceptance disposition

| Criterion | Verdict |
| --- | --- |
| Exact SHA / lineage / incoming 13 byte-identical to 16 | PASS |
| Five original findings repaired without weakening send/bridge/F7/M6/retention | PASS |
| Owner grant + CSRF + canonical bind + fail-closed races | PASS |
| Watermark preservation + memory-native eligibility | PASS (memory-native only) |
| Once per interval / lost-response receipt / overlap / cross-slot unresolved | PASS |
| Typecheck | PASS |
| Receipt/grant retirement for repeated intervals | FAIL as operational lifecycle; HOLD is documented and fail-closed |
| Source integration / Agent Branches / main restore | Head-owned, not performed |
| Physical M5 enrolled host + human/agent + reconnect + real native inbox | UNPROVEN |
| Issue 102 / M8b / M11 | OPEN |

Head may fast-forward this SHA to hub main after ordinary integration. This report grants no deploy, check enablement, native-effect, or production-config authority. Work declaration: `docs/reviews/CONVERSATION-PINGS-R2.md` released after commit; ignored review evidence retained for head checks.
