# M5 conversation pings review

**REVISE — exact proposal `1229be75432dc55f9939135d1a14a7e6e6e6a04b`.**

Independent reviewer: platform-pings-review, Codex/GPT-6 runtime family (backend minor version unavailable); author: Grok/grok-4.6. Findings reproduce on the exact reconstructed source. Source acceptance, integration and physical acceptance are separate gates.

The follow-up clears the monitored recipient's unread state and is authored as that recipient, so the native bridge suppresses it. Admin configuration also permits writing as another human without that human's session or an explicit delegation. Source acceptance is withheld. Integration requires the accepted CFB source and owner lease; physical M5 remains UNPROVEN. Issue 102, M8b and M11 remain OPEN.

## Exact inputs and reconstruction

Only immutable Git objects were used: main `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`, CFB `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, then the exact M5 delta. No peer dirty files, history, dependencies or Windows lane were copied. Existing dependencies were linked only. Actual tracked application files remain unchanged.

| Artifact | Verified SHA-256 / count |
| --- | --- |
| CFB patch | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` |
| CFB compact JSON manifest | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`, 199 files |
| M5 patch | `9d9c16582c6837a24f6ab771061df71803fcb35fb50b4822df8d3be403ac5f6c`, 55,419 bytes |
| M5 proposal Markdown | `6f08d9f07913cb02b852ed8626a8e3683c0ab06e636b32250444dfc0f207eb41` |
| M5 compact JSON manifest | `11841d06d59ce20d92e79f38f59092dd2b40353ad5ebecf5b5d1567c4e2a1b96`, 200 files |

Independently checked all 88 CFB input/output table rows against base/reconstructed bytes, all seven M5 output rows, 193 unchanged CFB files and 131/131 actual tracked base files. Complete actual manifests remain in ignored review evidence. The current dependency verdict is CFB SCOPED ACCEPT `5a4b9381d4863d10a1d46b249c0be83e17cee18f`; stale proposal dependency wording does not override it. The CFB review-header metadata errors do not change that source verdict: actual proposal parent is `f9f6fb258e5abdf4d97e9292043828b4cc80ac82`, base is `5dcdf716`, and author is GPT-6 rather than GPT-5.4.

Verified actual M5 delta (six modified, one new):

| Path | CFB input SHA-256 | M5 output SHA-256 |
| --- | --- | --- |
| `docs/GENERIC-HUB.md` | `67c5e01c44591a53bc328a228480542cadfc7559bdae08e420017085a3023331` | `12de3446bffe8c8e3cca5da1007d275d1ca4df543a8718d783bd22c9e81dc1be` |
| `src/checks.ts` | `4146fae3cc1695e2cb32bc7916d0f4aaef3f4649ea3184cc194378a20f5beb67` | `1455d28e84f84b7b031bb5df168082372c9694098badf1d2c859a266bd274fef` |
| `src/conversation-hub.ts` | `948457385592374ab455523a5727614fcff1c12661c954b707e011c8cf0e0b72` | `8012ce5f44a7ccb15ed075707028acd64d3912bf3cffe9d6fc0288c2f2f6688d` |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | `f2e99719ff84bfb7bc59346637f0b90dd8b70d193dd899b8ecd4fe2a2ecb8b06` |
| `src/hub.ts` | `d0444b58d216bf434798e5fd466550aa1b8c7cb5fe96700dddd57c75c2020ce7` | `848f814691e28d0a2fab21dc5fc43c0c2571069525c1ae4a884cce18ed806b20` |
| `test/checks.test.ts` | `5529b290755ecf05ed830bc96350c920a8d379a8a7f20060128bf937c3c8eea2` | `0725bce1433164148c1bc825178cf5af26cf52730fa33696c91d763bafeda2b9` |
| `test/conversation-pings.test.ts` | `new` | `0789b19c755d3a3707b4af61904c6119b7923d006fa6dd85e12bf8003cd54612` |

## Confirmed findings

### High: follow-up acknowledges its recipient and cannot reach that recipient's bridge

Reconstructed `src/checks.ts:209` chooses `params.actor`, falling back to `action.to`, as the sender. `src/conversations.ts:3677` posts through ordinary `sendMessage`, which intentionally advances the sender's read watermark at `src/conversations.ts:3022`. `host/agent_hub_host/conversation_bridge.py:447` suppresses self-authored rows.

Independent public Worker HTTP reproduction uses public device code/approval/token flow for an ordinary machine-bound agent, a signed local human session, public conversation creation/send, local SQLite timestamp aging, public admin+CSRF `PUT /api/checks`, and public forced run. Before: recipient read watermark 0, one retained overdue unread. After: two retained rows, follow-up sender equals recipient, read watermark 2, subsequent backlog PASS/zero overdue. The original unread remains retained; it has been silently marked read. The ping is ineligible for that recipient's bridge.

Fix: distinguish the authorized automation sender from the monitored recipient, preserve the recipient watermark, and prove a native-compatible receipt without borrowing recipient identity. Do not weaken ordinary send or bridge self-message rules.

### High: configured actor is treated as authority without an identity grant

`src/conversations.ts:3316` resolves arbitrary configured humans from `users`, synthesizes a session-type principal with read/send scopes and `csrfVerified:true`; `postScheduledFollowUp` passes only a caller context onward. No target session/delegation is supplied. Agent resolution similarly selects a live credential by identity rather than an explicit automation grant.

Public reproduction: a third admin cannot GET a private two-human conversation (403), but can configure a check naming one member, run it, and cause a note attributed to that member. No member session/token is supplied to the configuration or run. A human whose `users.is_admin` becomes 0 is refused by the ordinary conversation route (403), yet remains usable for a scheduled write. This is misleading human attribution and bypasses current human authorization, not merely admin permission to configure automation.

The same bypass persists when human disable is injected after a successful backlog evaluation and before action. A bare configured `recipient` also promotes to `review:recipient` and sends; it does not require the immutable qualified identity.

Fix: define and validate explicit automation authority, retain truthful sender provenance, require the current canonical identity, and revalidate the actual current grant/session/user state at effect time. Keep F7 credential and membership checks; their existence alone is not delegation.

### Medium: lost-response replay with changing detail cannot recover the committed receipt

Using real public forced runs and an injected response loss after the real durable send: the first action records failure; the follow-up changes read/backlog state. With `on:always` and a `{detail}` body, a same-slot retry computes a different body under the same idempotency key and returns `action failed: idempotency_conflict`. Exactly one row exists, but the committed effect has no recovered success receipt. Without `on:always`, the changed PASS result can skip the retry altogether.

Fix: durably bind the slot's effect intent, body and receipt, reconcile a lost response before reevaluation, and never report delivery merely from a successful message write.

### Medium: overlapping due snapshots run a later check twice

`src/checks.ts:131` loads all due rows before awaiting the first action; the unconditional update at line 137 does not claim a later row against a concurrent runner. Independent real-SQLite scheduler probe pauses the first row at an awaited action, starts a second `runDue`, then releases the first. The later conversation check evaluates twice in the same interval. One send is committed; the second run records PASS with `action failed: idempotency_conflict` because `{detail}` changed. This does not prove duplicate message rows, but it disproves the claimed once-per-interval execution and leaves conflicting run receipts. The slot implementation is inherited; M5 newly relies on it for conversation effects.

Fix: acquire each due slot with a durable conditional claim checked immediately before evaluation; bind retries to the stored intent/receipt. Cover multiple due rows, overlap, forced retry, replacement and restart together.

### Medium: required typecheck fails in the added fixture

`tsc --noEmit` fails at new `test/conversation-pings.test.ts:84`: the `Env.HUB` base-Hub stub is incompatible with the callback's ConversationHub type. Candidate runtime tests pass; that does not make the typecheck pass. Correct the test's production binding typing without suppressing typechecking.

## Independent verification

| Command / boundary | Result |
| --- | --- |
| `tsc --noEmit` | FAIL, new fixture type error above |
| `vitest run test/conversation-pings.test.ts test/checks.test.ts` | 22/22 PASS |
| Inherited 12-file regression cohort | Vitest 206/206 PASS; supervisor resource failure recorded below |
| M6 bounded replay in three disjoint describe groups | 26 + 16 + 19 = 61/61 PASS, each process exit 0; repeats excluded from unique totals |
| F7 `registry-f7.local.ts` with its composed Worker config | 14/14 PASS |
| Separate public HTTP / real SQLite probes, final run | 22 PASS / 8 FAIL, 30 total; exit 1 |
| Actual bridge cycle with fake-native, memory-only contract | 1 PASS / 1 FAIL; actual ping yields zero deliveries, distinct-sender control yields one |

The 12-file cohort consists of liveness, hub, m3-bus, claim-admission, conversations, conversation-security, conversation-retention, conversation-integration, conversation-platform, machine-pairing, agent-registry and registry-r2. Expected injected SQLite retirement errors appear in retained logs; they are not hidden failures. No host/UI rerun is credited or needed for this TS-only delta.

The eight final adversarial failures are: target watermark, target sender/bridge visibility, outside-admin human impersonation, disabled human before evaluation, disabled human after evaluation, lost-response receipt recovery, overlapping later-row execution, and bare-name promotion. They substantiate the findings above rather than eight unrelated defects.

Passing adversarial controls cover ordinary-agent administration denial and read-only conversation-write refusal, admin CSRF, sender override rejection, current/read/self-sent messages, retained unread after prefix/interior gaps, TTL cleanup, a hidden 51st conversation, missing/invalid/throwing conversation dependencies, and removal/archive/credential revocation/expiry/machine deletion/registry removal both before execution and after successful evaluation. The scan cap returns error rather than false healthy. No real credential is stored in checks or new secret endpoint added. `DEFAULT_CHECKS` remains liveness-only; no scheduler was added. Existing fleet/ordinary-agent routes remain covered by the inherited M6/F7 cohorts.

The bridge contract imports the exact reconstructed Python implementation and consumes history captured from the real public HTTP probe. It replaces journal/member/native methods with memory-only test doubles, runs `cycle`, and observes cursor 1→2 with zero delivery for the actual self-authored ping. It is a contract falsifier, not native identity or physical delivery proof. No bridge native CLI, delivery inbox or journal is touched; required reviewer coordination uses the native mailbox.

Retained reviewer harness failures are explicit: the first 16-case run had 10 passes, five behavior failures and one fixture error from setting non-nullable `registered_at` to NULL. That fixture was corrected to delete only the local test machine; the next run had 11 passes/five behavior failures. A later 24-case run retained an injected method between cases; its after-evaluation/overlap results are superseded by the final 30-case run, whose `beforeEach` deletes the override. Original failures/logs remain. No candidate source/test was edited. Candidate security negatives remain explicit. The candidate follow-up positive test expects target-authored rows and misses the destructive read-state/bridge effect. Historical author failure logs were not found in its stated evidence directory; only source manifests were present. The head was asked for their exact retained location; absence is not proof that assertions were preserved historically.

## Reproduction and retained evidence

From a clean copy containing the immutable proposal objects, reconstruct under an owned ignored path:

```bash
mkdir -p .local/review/source .local/tmp
git archive 5dcdf7164b28bd0e3a1d717ac9090c054eca3f24 | tar -x -C .local/review/source
git show cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch > .local/review/cfb.patch
git show 1229be75432dc55f9939135d1a14a7e6e6e6a04b:docs/proposals/CONVERSATION-PINGS.patch > .local/review/m5.patch
git apply --check --directory=.local/review/source .local/review/cfb.patch
git apply --directory=.local/review/source .local/review/cfb.patch
git apply --check --directory=.local/review/source .local/review/m5.patch
git apply --directory=.local/review/source .local/review/m5.patch
```

Use only an existing dependency installation by symlink; isolate HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME, XDG_DATA_HOME and TMPDIR before invoking any test. Set `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1` and run each command under `taskset -c 0`. No install/importer/native driver is needed.

In the retained assigned workspace, these independent commands use the preserved isolation/resource wrapper and configs:

```bash
python3 .local/review/run.py typecheck node_modules/.bin/tsc --noEmit
python3 .local/review/run.py candidate node_modules/.bin/vitest run --config ../review.config.ts test/conversation-pings.test.ts test/checks.test.ts
python3 .local/review/run.py f7 node_modules/.bin/vitest run --config ../f7.config.ts test/registry-f7.local.ts
python3 .local/review/run.py boundary-r4 node_modules/.bin/vitest run --config ../probe.config.ts
python3 .local/review/run.py native-contract python3 ../probes/native-contract.py
```

Use fresh log names on rerun to retain the original receipts. Private evidence is relative to `.local/review`: `manifest.json`, `probe-manifest.json`, `probes/m5-boundary.test.ts`, `probes/native-contract.py`, captured non-secret local history, both patches, configs, isolation/resource wrapper and per-run logs/metrics. Logs from failed/aborted attempts are preserved. Actual source manifests are rechecked after testing. Final independent probe SHA-256 is `1ebb07f2b2708fa6b3bb5aac34ea6d2f3aecd869b7282646d0b9902b72482941`; bridge contract SHA-256 is `f073c7688f85110f9c05b6ec1efbc185338edebcb5333c3b47c8a0eb356b96e1`.

The public-HTTP reproduction needs only local seeded humans/machine. Positive agent tokens are obtained through the real public device flow, never SQL credential insertion. Aging/deleting only local SQLite rows supplies deterministic backlog/gap/race fixtures. Configure via public admin+CSRF `/api/checks`; force via `/api/checks/followup/run`; compare target GET history/read state and SQL rows before/after. For identity reproduction, use a third admin whose direct GET of the conversation is 403, configure another human member's actor, and inspect that member's attributed note. For after-evaluation races, the separate probe wraps only the dependency callback between successful evaluation and the real effect; this is injected timing evidence, not a physical concurrency claim.

## Resources and limits

Only one CPU, UV/V8/Go pools of one, isolated HOME/XDG/TMPDIR, existing dependency symlinks and real Worker SQLite were used. No production or bridge-native effects, external agent/model launches, installs, deploys, private token/task/config manifests or external registry importer were accessed. Probe fixture credentials are local-only and never printed in this report.

Resource exception: the regression supervisor sampled scratch above 30 MiB and returned 90 even though its retained Vitest log reports all 206 cases passing. Exact peak is UNKNOWN because the next small probe overwrote that metrics file; runtime scratch had automatically disappeared by inspection. This is a review-process limit breach, not an M5 source finding. Head was notified. Logs remain; subsequent runs use per-run metrics and an earlier process-group termination guard. No assertion or failure is skipped or relabelled as a pass.


A later full-file M6 confirmation run was terminated by the revised early guard, exit 143, at sampled 26,144,768 allocated / 25,521,153 apparent bytes (below 30 MiB). Three smaller disjoint describe selections then passed 26/16/19 cases with clean exits: all 61 unique M6 case names are retained in `m6-case-manifest.json`. Each selection's skipped cases are exactly the cases run by the other selections; no failed case was excluded. These repeats were performed to resolve the supervisor-exit uncertainty and add no unique case count. Their measured allocated peaks were 19,640,320 / 16,027,648 / 19,349,504 bytes. Final 30-case probe peak was 11,300,864 allocated / 10,728,113 apparent bytes; F7 peak was 11,108,352 / 10,565,046 bytes. The original overrun remains a breach with unknown exact peak.

At final measurement, assigned review/tmp together used 6,680,576 allocated / 5,962,066 apparent bytes. Session cgroup recorded memory peak 711,413,760 bytes and task peak 96, with configured limits 1,572,864,000 bytes / 256 tasks. MemAvailable was 39,672,754,176 bytes and disk available 77,954,891,776 bytes at the last host reading. These are measured observations, not guarantees about unsampled scratch growth or other sessions' aggregate use. The head retains aggregate 512 MiB custody.

## Acceptance disposition

| Criterion | Verdict |
| --- | --- |
| Exact artifact/table/source reconstruction | PASS |
| Distinct conversation backlog, bounded scan, retained-unread semantics before action | PASS with the tested controls above |
| Safe follow-up that preserves unread and can reach target native inbox | FAIL |
| Truthful sender identity and current authorization/delegation | FAIL |
| Once per interval and recoverable durable effect receipt under overlap/retry | FAIL as described; one message row observed, not duplicate-row proof |
| Required typecheck | FAIL |
| Current M6/F7/API retention and existing public privilege boundaries | Tested regression cohorts PASS; scheduled human bypass remains a finding |
| Source integration / lease / main restore | Head-owned, not performed by this reviewer |
| Physical M5 enrolled host + human/agent exchange/reconnect/native receipt | UNPROVEN; operations not authorized here |
| Issue 102 / M8b / M11 | OPEN |

Head should route the five source findings to a separate implementer, preserve the accepted CFB bytes and ordinary sender/bridge semantics, then request different-model review of the new exact proposal. This report grants no source integration, deployment, production configuration or native-effect authority. All private review evidence remains retained until head verification.
