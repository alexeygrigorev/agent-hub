# M5 conversation backlog and safe follow-up

Task platform-pings. Author: native UUID `ace5d868-218b-4eed-bbd2-79d09e6ab869`, tag `platform-pings`, engine grok, model grok-4.6. Actual Agent Branches task/base are owned by head (fork/WIP/main). This worktree is fresh origin/main `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`. This proposal produces only two inert artifacts. Actual tracked application files remain unchanged.

**Disposition: prepared for independent exact-version review, not accepted or integrated.** No source-owner handoff, source application, push, deployment, production enrollment, native ping or physical host recovery is claimed. Combined conversation assembly `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` remains PROVISIONAL. Independent Argon `4aab5128` is still ACTIVE against that exact cfb source. M5 cannot be accepted or applied until cfb review, source handoff and M1–M3 integration. If head reports dependency REVISE, this delta is rewritten against the new pin only.

Portable [patch](CONVERSATION-PINGS.patch): SHA-256 `9d9c16582c6837a24f6ab771061df71803fcb35fb50b4822df8d3be403ac5f6c` (55419 bytes), against reconstructed cfb source (main `5dcdf716` + cfb patch `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10`), not against bare main. Fresh archive + cfb patch + this delta reproduces every output hash below.

## Gates (report separately)

| Gate | Status |
| --- | --- |
| M5 SOURCE (Worker SQLite/HTTP integration) | PASS on reconstructed cfb + this delta. Evidence below. |
| M5 PHYSICAL (named enrolled host + human/agent conversation, ordered messages after reconnect, native inbox receipt) | UNPROVEN. Runbook only. No native effects created. |
| Contest issue 102 / M8b fenced principal-wake | OPEN. This milestone does not close them. |
| M11 physical Windows / second host | OPEN. |
| Production checks enabled | Not done. Default `liveness` seed is unchanged. |

## Immutable inputs

| Component | Pin |
| --- | --- |
| Accepted Hub/M6 main | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` |
| Combined conversation assembly (PROVISIONAL) | `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc` |
| cfb patch vs main | `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10` |
| cfb 199-file freeze | `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a` |
| Active cfb Argon | `4aab5128` |

Reconstruction used git archive of main plus the cfb patch from git objects. No peer worktree, dependency tree, dirty file or history was copied. Existing `node_modules` was reused by symlink to `/home/alexey/git/agent-hub/node_modules` only.

## Semantic design

Existing `inbox_backlog` counts Hub 1:1 bus `messages` with `acked_at IS NULL`. Conversation unread lives on `conversation_members.last_read_seq` and `conversation_messages.seq` inside ConversationHub SQLite. This delta adds a distinct check kind `conversation_backlog` rather than renaming the bus check.

1. `ChecksService` gains kind `conversation_backlog`. Optional `params.conversation_id` is allowed only on that kind. Optional `action.conversation_id` is allowed only on `type: "message"`. Existing kinds, bus `message` actions, launch/fleet placement, liveness and quota are unchanged.
2. Trusted port `ConversationCheckPort` is `{ evaluateBacklog, sendFollowUp }`. `Hub.runChecks` spreads `this.extraCheckDeps()`. Base Hub returns `{}` (7-line hook). `ConversationHub` binds the live `ConversationsService`. No public auth bypass, forged hub/system/schedule principal, plaintext token or invented private endpoint.
3. `ConversationsService.evaluateScheduledBacklog` resolves `params.actor` at effect time through F7 (`credentialActor` / `registry_actor`, project-qualified dual-tag, reserved namespace reject, live unrevoked unexpired agent credentials, `users` for humans). Current membership and participant visibility bound the scan. Archived conversations are excluded. Overdue means retained `seq > last_read_seq AND created_at < now-max_age_s AND sender_actor != actor`. A retention gap is not treated as read or delivered; pruned rows disappear, remaining retained unread still fail. Scan limit 50; truncated-with-zero-overdue is `error` (`scan_truncated`); truncated-with-overdue still `fail`. Missing/ambiguous/deleted member, unknown conversation, revoked/expired credential, reserved actor and invalid responses fail closed (`error`), never false-healthy.
4. Optional follow-up posts exactly one conversation message through existing `sendMessage` membership, CSRF-verified trusted caller, and idempotency `(conversation_id, sender_actor, idempotency_key)` with key `check-<name>-<slot>`. Slot claim still happens before evaluate/act. `replace()` still preserves `next_due_at` by name. Overlapping cron, retries and response-loss replay the same key. Membership or credential revocation between evaluate and act prevents the write. If `sendFollowUp` throws after await, `act` records `action failed:` and does not return `conversation_message <id>`.
5. No new scheduler, service, timer, terminal write, provider/agent launch or principal wake. Notification effects stay conversation/durable inbox only. `DEFAULT_CHECKS` remains the seeded liveness row.

## Output hashes (reconstructed cfb + this delta)

M5 tree: 200 regular files, digest `11841d06d59ce20d92e79f38f59092dd2b40353ad5ebecf5b5d1567c4e2a1b96`. 193 of 199 cfb files are byte-identical. One new file.

| Path | SHA-256 |
| --- | --- |
| `docs/GENERIC-HUB.md` | `12de3446bffe8c8e3cca5da1007d275d1ca4df543a8718d783bd22c9e81dc1be` |
| `src/checks.ts` | `1455d28e84f84b7b031bb5df168082372c9694098badf1d2c859a266bd274fef` |
| `src/conversation-hub.ts` | `8012ce5f44a7ccb15ed075707028acd64d3912bf3cffe9d6fc0288c2f2f6688d` |
| `src/conversations.ts` | `f2e99719ff84bfb7bc59346637f0b90dd8b70d193dd899b8ecd4fe2a2ecb8b06` |
| `src/hub.ts` | `848f814691e28d0a2fab21dc5fc43c0c2571069525c1ae4a884cce18ed806b20` |
| `test/checks.test.ts` | `0725bce1433164148c1bc825178cf5af26cf52730fa33696c91d763bafeda2b9` |
| `test/conversation-pings.test.ts` | `0789b19c755d3a3707b4af61904c6119b7923d006fa6dd85e12bf8003cd54612` |

`src/hub.ts` is a 7-insertion / 1-deletion hook (`extraCheckDeps` + spread). Hub owner still reserves `src/hub.ts` / `src/env.ts` / liveness. Apply that hunk only with the owner's ACK.

Actual worktree tracked files: 131/131 byte-identical to `5dcdf716` before and after this commit.

## Reproduce

Use an unused owned scratch directory. Do not apply to tracked source, install dependencies, or create a new Git workspace.

```bash
repo=$PWD
scratch="$repo/.local/tmp/review-m5"
mkdir -p "$scratch" "$repo/.local/tmp"
git archive 5dcdf7164b28bd0e3a1d717ac9090c054eca3f24 | tar -x -C "$scratch"
# cfb patch is the committed two-artifact assembly input, not this repo
git apply --whitespace=nowarn --directory=.local/tmp/review-m5 /absolute/cfb/CONVERSATION-INTEGRATION.patch
git apply --check --directory=.local/tmp/review-m5 docs/proposals/CONVERSATION-PINGS.patch
git apply --directory=.local/tmp/review-m5 docs/proposals/CONVERSATION-PINGS.patch
ln -s /home/alexey/git/agent-hub/node_modules "$scratch/node_modules"
cd "$scratch"
export UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1
export TMPDIR="$repo/.local/tmp"
taskset -c 0 npx vitest run test/conversation-pings.test.ts test/checks.test.ts
```

200-file digest after both patches:

```python
from pathlib import Path
import hashlib, json
root=Path('.')
rows=[]
for p in sorted(root.rglob('*')):
    rel=p.relative_to(root)
    if not p.is_file() or p.is_symlink(): continue
    if any(x in rel.parts for x in ('.local','node_modules')): continue
    if rel.parts[0] in ('dist','.wrangler'): continue
    rows.append([str(rel), hashlib.sha256(p.read_bytes()).hexdigest()])
assert len(rows)==200
assert hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()=='11841d06d59ce20d92e79f38f59092dd2b40353ad5ebecf5b5d1567c4e2a1b96'
```

Do not run `test/test_registry_import.py` (hard-coded TEAM-REGISTRY importer). Isolate TMPDIR under the worktree `.local/tmp`.

## Source evidence

Commands used CPU0, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, owned TMPDIR. One core bounded real Worker SQLite. No skipped assertions, no permissive membership fixtures, no ephemeral non-SQL store.

| Check | Result |
| --- | --- |
| `test/conversation-pings.test.ts` + `test/checks.test.ts` | 22/22 PASS, 3.81s |
| `test/liveness.test.ts` `test/hub.test.ts` `test/m3-bus.test.ts` `test/claim-admission.test.ts` | 107/107 PASS, 18.94s |
| Fresh 5dc + cfb patch freeze | 199/199 `17aab235…` |
| Fresh 5dc + cfb + M5 apply | 7 output hashes match; 200-file digest `11841d06…` |
| Tracked non-artifact files | 131/131 identical to main |

Adversarial cases covered in `test/conversation-pings.test.ts`: bus ack is not conversation unread; overdue retained unread vs read/current/self-sent/pruned; retention gap does not advance `last_read_seq`; F7 project-qualified, dual-tag ambiguous, reserved, expired and revoked credentials fail closed; one authorized follow-up per interval with idempotent replay; membership remove between evaluate and act blocks the write; throw after await does not claim `conversation_message`; `replace()`/restart preserve `next_due_at`; unauthorized Bearer PUT `/api/checks` is 403; invalid `max_age_s` is 400; admin+CSRF configure/list works; `/api/liveness` without auth stays 401; Hub.runChecks routine posts one conversation message and replays the same idempotency key; archived/unknown conversation fail closed. `checks.test.ts` still rejects malformed definitions, keeps inbox_backlog bus behavior, and fails closed when the conversation port is missing.

Pre-existing M6 `retirement linkage failure: SQLITE_CONSTRAINT` uncaught diagnostic still appears during claim-admission tests and is not an M5 regression; those tests passed.

## Failures

None on the M5 source suite. Physical M5 was not attempted.

## Remaining integration and physical dependencies

- cfb Argon `4aab5128` ACCEPT plus source-owner handoff of the combined conversation tree onto main.
- M1–M3 conversation source actually integrated; this delta then rebases if the pin moves.
- Hub-owner ACK for the `src/hub.ts` hook.
- Operator configuration of `conversation_backlog` / routine conversation `message` on an already-authorized host. This proposal does not enable it.
- Domain `hub.dtcdev.click` and host tokens are operations dependencies. If the enrolled origin is workers.dev, say so; do not pretend the custom domain exists.
- Physical M5: named genuinely enrolled host, named human+agent conversation, ordered messages after reconnect, native inbox receipt, private evidence path, public STATUS sanitized SHA/time only.
- Issue 102, M8b, M11 remain open.

## Owner handoff / runbook (already-authorized host)

Do not deploy, read credentials, enroll hosts or create native inbox effects from this worker.

1. After cfb+M5 review ACCEPT, Hub owner applies this patch onto the reconstructed cfb tree in a private checkout, runs the vitest commands above, then integrates through the normal head path.
2. Rollback of unintegrated artifacts: drop the two proposal files; tracked source is already unchanged. Rollback after a future apply: `git revert` the integration commit, or `PUT /api/checks` with the previous document (default liveness only). No extra timer/service to stop.
3. Optional later physical proof, by the operations owner on an already-enrolled host: configure one `conversation_backlog` for a real conversation id and actor with `max_age_s >= 60`, action `message` with that `conversation_id`, interval >= 300. Confirm one follow-up body in Hub conversation history, native inbox receipt after reconnect, and that a second cron in the same slot does not duplicate. Record private evidence; publish only SHA/time. Disable the check afterward.

Example configuration (do not upload from this milestone):

```json
{"checks": [
  {"name": "liveness", "kind": "liveness", "interval_s": 300},
  {"name": "alice-unread", "kind": "conversation_backlog", "interval_s": 900,
   "params": {"actor": "human:<sub>", "max_age_s": 3600, "conversation_id": "<ulid>"},
   "action": {"type": "message", "to": "human:<sub>", "body": "follow-up from {check}", "conversation_id": "<ulid>"}}
]}
```

## Minimal future owner scopes

After ACCEPT, apply only:

- `src/checks.ts`
- `src/conversations.ts` (scheduled backlog/follow-up methods)
- `src/conversation-hub.ts` (`extraCheckDeps`)
- `src/hub.ts` (`extraCheckDeps` hook; Hub owner)
- `docs/GENERIC-HUB.md` (scheduled-checks table)
- `test/checks.test.ts`
- `test/conversation-pings.test.ts`

Do not touch `src/env.ts`, liveness internals, worker cron wiring (existing `runChecks` already runs), public UI, host CLI, secrets, or production check documents.

## Resource

Worker memory cap 1500 MiB / 256 tasks. Private `.local` aggregate stayed well under 45 MiB (peak observed ~18 MiB with baseline+verify copies). CPU0, pool size 1. Scratch and TMPDIR under this worktree `.local/tmp`. Probe logs/manifests retained in `.local/pings/` until head checks.
