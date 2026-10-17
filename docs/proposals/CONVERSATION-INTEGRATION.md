# Combined conversation platform: early executable integration checkpoint

Task `0414` / `platform-conv-int-0414`. Private inert proposal only. Hub source ownership has not been handed off. This is integration preparation toward M1/M2/M3/P3/public pairing; full M2b/M4–M11 remains required. No main push, no deployment, no production enrollment.

**Disposition: early coherent checkpoint, not accepted.** Independent review of this exact artifact and acknowledged source handoff are still required. Head owns subsequent main/canonical restore.

Author session: `51db3a5a-ab14-4bbc-9be2-183be9d23eda` (`platform-conversation-integration`, engine grok). Verified `aplexer whoami` workspace `/home/alexey/git/agent-hub-wt-platform-conversation-integration`. Tracked HEAD parent `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`.

Portable patch: [CONVERSATION-INTEGRATION.patch](CONVERSATION-INTEGRATION.patch), SHA-256 `eb6706c1e4c5a5ed5cfc25c55481846f1e785da35af825994ea125103106256d`. `git apply --check` on a fresh main archive succeeds. 76 paths (61 new, 0 deleted). Tracked `src/` `host/` `public/` `test/` `config/` in this worktree remain byte-identical to main. Product `public/conversations.js` SHA-256 `208275feacfce835a27f275c4af00b6da5c8d95797f4d4cd2265421b532c97e1` matches transport `d9b4`.

## Pin status (do not treat passing tests as acceptance)

| Input | SHA | Status |
| --- | --- | --- |
| Hub main | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | accepted M6 |
| Transport | `d9b4cde07e3718d77b2db9308cd323a5c8e003ed` | scoped Argon `8ecac25e021641781f01c18d662d130812797eac` |
| API | `8dc9235ce246ff8316048fa837b34b53ae7a0cdd` (parent `7c004676dc9c0c6496656cb6a4bc62dbadff09ae`) | **scoped ACCEPT** `8ca59dc1b67d584aff724a50deef450f66a08af1` (394/394 + typecheck, excluding F7). Outer `transactionSync` wrap around composed `runFairRetentionPass` (nested depth joins the open outer txn) and m3-http slot-clock fixture. Dirty author files are not imported. |
| F7 registry | `ddaf80d82022736da82ff11d521c6a9f92ff593e` patch SHA-256 `a3b2c2fdcc0feb6287e8d8695edd19137687ffe82967c66cc25105491e049b16` | Argon ACCEPT `426fdf7` |
| P3 gap | `0896ebe157cc732342b17fb40a6cf6e096ef93a2` | scoped ACCEPT `f19353e8c9a900f2697f6edeb7da08691cf901e8`. Frozen 17424 contract. Default HOLD. No automatic `max(server read)`. |
| Public pairing | R2 FINAL `eee4d2f88305a6e64545c8e7b68f329a162811d1` (via `6dc1919395ca079de237b1ab3864db989972ce52`, parent `1149f65`) patch SHA-256 `3b9d5a9583d0daa4ac1ef7f1dddf603f9b9b5c7550b995a78633bef958219f10` | **PROVISIONAL.** Independent `platform-pairing-r2-review` is reviewing exact `eee4`. Closes the two MEDIUM defects reported on `1149` (token/refresh `machine_id`; approval TOCTOU). This combination does not invent further pairing semantics. |

Final base must wait for independent acceptance of pairing `eee4` (or a later repaired SHA). API `8dc` is credited only via scoped ACCEPT `8ca59dc1`. This checkpoint combines those committed pins so that rebase stays mechanical. M2b `d80` is excluded.

## Native journal scope (paused)

Frozen 17424 writes `workspace/.local/conversation-bridge-<hash24>.json`. Prospective scope join **C-0414-JOURNAL** adds only those two journals and their empty `.lock` files to this worker's declaration (no `.tmp` present). Existing journals are **preserved in place**; they were not deleted, reset, or resent. Head read-only reconciliation matches this inventory: both journals have zero unresolved pending effects. Native cycles stay paused. No retroactive compliance claim.

| Basename | Fixture | Binding | Cursor / effects |
| --- | --- | --- | --- |
| `conversation-bridge-7411e5cb52a52f78bae03df8.json` (+ empty `.lock`) | `cint-local` interrupted | actor `cint-local:platform-conversation-integration`, UUID `51db3a5a…`, tag `platform-conversation-integration`, workspace this worktree, machine `cint-machine`, origin `http://127.0.0.1:36289` (dead) | cid `01M53SWSD4SV3CTVP5QRPQB9CM` cursor **3**. One delivery: seq1 human browser nonce, `attempted=true`, native_id `01a1479e-9541-7311-a9f7-828a89443260`. Seq2–3 are agent-CLI self-authored (`HUB_TOKEN` is the public-issued agent). `replies` empty: harness stopped after first `--once` cycle at a workspace assertion, before native reply. No gaps, no uncertain `native_id`. |
| `conversation-bridge-022b4ce849850fd68ddca9f4.json` (+ empty `.lock`) | `cint-local-r2` completed | same UUID/tag/workspace/machine; project `cint-local-r2`; origin `http://127.0.0.1:39703` (dead) | cid `01M53SYGCD3ARVH49H2EKBJK3S` cursor **4**. Seq1 human delivered (`01a1479f-7993-7033-b219-a8915aff3120`). Native reply `01a1479f-7e69-71d1-8c3e-ff6abeb032e0` flushed (`done=true`). Seq2–3 agent-CLI self-authored; seq4 self-authored mapped reply. No gaps, no pending uncertain effects. Second physical host untested. |

Private inventory without secrets: `.local/integration/journal-scope.json`. Own inbox leftover from those runs was ACKed so it is not unread; native log rows remain as evidence.

## Reconstruction (private tree; do not apply to tracked source)

From a checkout that contains the Git objects and already-installed dependencies. Symlink `node_modules`; do not install.

```bash
set -eu
repo=$PWD
base=5dcdf7164b28bd0e3a1d717ac9090c054eca3f24
transport=d9b4cde07e3718d77b2db9308cd323a5c8e003ed
api=8dc9235ce246ff8316048fa837b34b53ae7a0cdd
api_old=da38cb2a00c092423d0b67419b0ea45d6a09dfa9
f7=ddaf80d82022736da82ff11d521c6a9f92ff593e
p3=0896ebe157cc732342b17fb40a6cf6e096ef93a2
pairing=eee4d2f88305a6e64545c8e7b68f329a162811d1
scratch=.local/integration/tree
test ! -e "$scratch"
mkdir -p "$scratch" .local/tmp "$scratch/.local/tmp"
git archive "$transport" src host test config public package.json package-lock.json tsconfig.json vitest.config.ts wrangler.jsonc pyproject.toml \
  docs/CONVERSATION-BRIDGE.md docs/CONVERSATION-CLI.md docs/CONVERSATION-PLATFORM.md docs/CONVERSATION-UI.md docs/CONVERSATIONS.md \
  | tar -x -C "$scratch"
git show "$f7:docs/proposals/PROJECT-AGENT-REGISTRY.patch" > .local/integration/f7.patch
git show "$pairing:docs/proposals/MACHINE-BOUND-AGENT-PAIRING.patch" > .local/integration/pairing.patch
# Verify the two patch SHA-256 values in the table above before applying.
git apply --check --directory="$scratch" .local/integration/f7.patch
git apply --directory="$scratch" .local/integration/f7.patch
git apply --check --directory="$scratch" .local/integration/pairing.patch
git apply --directory="$scratch" .local/integration/pairing.patch
for path in docs/CONVERSATION-BRIDGE.md docs/CONVERSATION-BRIDGE-GAPS.md \
            host/agent_hub_host/cli.py host/agent_hub_host/conversation_bridge.py \
            host/tests/test_conversation_bridge_gap.py \
            test/conversation-bridge-gap-worker.ts test/conversation-bridge-gap.mjs \
            test/wrangler.conversation-bridge-gap.jsonc; do
  git show "$p3:$path" > "$scratch/$path"
done
# 3-way: ancestor da38/main, ours F7+pairing tree, theirs API 8dc (conversations.ts/bus.ts unchanged from 7c004).
git show "$api_old:src/conversations.ts" > .local/integration/conversations.ancestor
git show "$api:src/conversations.ts" > .local/integration/conversations.api
git merge-file -L F7-applied -L ancestor -L API-8dc9235 \
  "$scratch/src/conversations.ts" .local/integration/conversations.ancestor .local/integration/conversations.api
git show "$base:src/bus.ts" > .local/integration/bus.ancestor
git show "$api:src/bus.ts" > .local/integration/bus.api
git merge-file -L F7-applied -L ancestor -L API-8dc9235 \
  "$scratch/src/bus.ts" .local/integration/bus.ancestor .local/integration/bus.api
for path in src/conversation-hub.ts docs/CONVERSATIONS.md test/m3-http.test.ts test/conversation-retention.test.ts; do
  git show "$api:$path" > "$scratch/$path"
done
# Harness hunks in this patch: public device issuance (no SQL-seeded agent token);
# native qualify bound to the reconstructing session; mock UI waits for Ada
# thread load before releasing a held POST (same store through the switch).
deps=$(realpath "$(git rev-parse --git-common-dir)/../node_modules")
ln -s "$deps" "$scratch/node_modules"
cd "$scratch"
export UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 TMPDIR="$scratch/.local/tmp"
taskset -c 0 timeout 120 npm run typecheck
```

Alternatively apply [CONVERSATION-INTEGRATION.patch](CONVERSATION-INTEGRATION.patch) to a fresh main archive (`git apply --check` then `git apply`). That reproduces the same private tree, including harness hunks.

When an accepted pairing SHA lands, re-apply that patch in place of `eee4`; do not locally invent pairing semantics. API overlay stays `8dc` unless a later accepted repair pin is supplied.

## Overlaps and resolutions

Transport `d9b4` is main plus 49 conversation files (42 reviewed UI/CLI/API/dakit/docs/host files plus 7 integration files). Its six original API files match `da38`. Bridge files match frozen `17424` / `619ba499`.

1. **API vs transport conversations/bus.** `8dc` supersedes `src/conversation-hub.ts`, `docs/CONVERSATIONS.md`, `test/m3-http.test.ts`, and `test/conversation-retention.test.ts`. `src/conversations.ts` / `src/bus.ts` 3-way against F7 is unchanged from `7c004` (those two blobs are identical at `8dc`). `src/worker.ts` keeps the transport export: `ConversationHub` as public `Hub` under existing `HUB` / `idFromName('hub')` / SQLite. `8dc` still exports `Hub as ConversationHub` and now wraps composed retention in one `transactionSync`, with nested child txns joining the open outer txn.

2. **F7 vs API `src/conversations.ts` / `src/bus.ts`.** Same 3-way as the prior checkpoint: `git merge-file` exit 0. All F7 added lines and all API added lines are present.

3. **P3 vs transport native.** P3 final files overlay directly. Default HOLD remains. Remote read watermark cannot skip unresolved native effects.

4. **Pairing vs F7.** R2 patch `3b9d5a95` applies cleanly after F7. Output hashes match the eee4 manifest (`src/hub.ts` `51a488ab…`). Pairing remains provisional until `platform-pairing-r2-review` verdict.

5. **Harness.** `test/conversation-platform-worker.ts` seeds a human admin and enrolled machine; it never inserts an agent credential. `test/conversation-platform.mjs` issues the agent token through public `/api/device/code` + admin CSRF approve with `machine_id` + `/api/device/token`. Native identity is this session. Frozen 17424 journals still land at `workspace/.local/conversation-bridge-*.json` (see journal scope). `test/conversations-ui.browser.mjs` keeps every original isolation assertion. The mock `messageStore` is reset between tests and is the same store through the within-case account switch, including the held POST commit. The only added wait is Ada's `.msg__body` after chrome and before `hold.release()`, so her initial history GET cannot fulfill after that commit. Product `conversations.js` is unchanged.

No F7 migration/credential/namespace/M6 fix was dropped. No original assertion was weakened to combine.

## Characterizing verification (not acceptance)

CPU 0, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, TMPDIR under owned `.local`. Existing Playwright via `PLAYWRIGHT_PACKAGE`. Symlinked `node_modules`. Peak scratch under 30 MiB. Tracked source hashes unchanged. Native cycles were not repeated in this checkpoint.

| Check | Result |
| --- | --- |
| Typecheck on ACCEPTED API `8dc` + pairing `eee4` combination | PASS |
| Pairing/F7/claim/conversations domain on that tree | 117/117 (4 files; expected M6 retirement SQLITE_CONSTRAINT_TRIGGER noise) |
| `test/conversation-retention.test.ts` + `test/m3-http.test.ts` | 29/29 |
| Mock-API UI desktop/narrow/theme, original assertions | **Old-order unmodified mock: 28/29 twice** (`#conv-detail` included `Grace held send`; `sendLog.length===1`; outbox null; Ada chrome; bodies=3). Isolated two in-flight tests on that same unmodified mock: **2/2**; diagnostic showed store 2→3 at release and DOM staying 2 (client 201 dropped). Full-suite fail is Ada's in-flight history GET fulfilling after the held POST commit (same store, slower after 15 prior tests). `resetFixtures` already replaces `messageStore` between tests. **Narrow hunk:** wait for Ada `.msg__body` before `hold.release()`; store still commits; assertions unchanged; product bytes unchanged. **Then 29/29.** |
| Prior Worker/browser/CLI/public-token characterizing | PASS on `7c004`+`1149` combination (7 checks). Not re-run after `8dc`/`eee4`; native cycles paused. |
| Genuine own native `cint-local-r2` | PASS on `7c004`+`1149`; journals preserved as known settled effects; further native cycles paused. |

## Combined hashes for merge products

SHA-256 of the private tree after reconstruction.

| Path | SHA-256 | Origin |
| --- | --- | --- |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | 3-way F7 + API (identical at 7c004 and 8dc) |
| `src/bus.ts` | `6300681ab61a275bfccc18912803c2b96dcfd484dbd66c6e048dc12f54d626f5` | 3-way F7 + API (identical at 7c004 and 8dc) |
| `src/conversation-hub.ts` | `948457385592374ab455523a5727614fcff1c12661c954b707e011c8cf0e0b72` | API `8dc` |
| `src/worker.ts` | `f0b067647397e4bd7f263e868787783d6b61d4bfe21d07eb3b9b1fac36ab1adc` | transport |
| `src/agent_registry.ts` | `5aa252d73029b51f4852a7850a02cf24d4b197b4a2e235a366862f99a49c0a3e` | pairing |
| `src/hub.ts` | `51a488ab738710dfab00842f5714245e9acaac4574046115d37d197018b85002` | pairing eee4 |
| `src/claim_admission.ts` | `5b6b728de08f32784f03c9a4123356b25fce2425a886a83f2be392b4e63b3dc5` | pairing |
| `host/agent_hub_host/cli.py` | `7a5bda4887e8f93896c941bf5b220759eb1aace4dfdc1e33031c381f1eb11c32` | P3 |
| `host/agent_hub_host/conversation_bridge.py` | `7e7ebaec3148b4cb5522bdead67e171f9adbcbb8e6c0d94085813c80ee313a4e` | P3 |
| `test/conversation-platform.mjs` | `c53cab3e19e2c7f86c72587fdb9a3d5cb80f668bd117fb7adfeb6ef450ecad33` | harness hunk |
| `test/conversation-platform-worker.ts` | `b063ff38ed83d26445f24d34ff6ff559df77b48b4e1d35ac64109f01fb3bad5b` | harness hunk |
| `test/conversations-ui.browser.mjs` | `447252dec697e9ede91e43b85357f9c49072cc9528f6125f1d43543718ec53b6` | harness hunk (wait Ada thread before late 201) |

## Remaining gates

- Independent acceptance of pairing `eee4` (or a later repaired SHA); do not invent pairing semantics here.
- Distinct-model review of this exact proposal SHA after pairing verdict.
- Native public-token Worker/browser/CLI/own-native route only after pairing review and further bounded head instruction. Existing journals stay preserved as known settled effects.
- Acknowledged Hub/registry/M6/test owner leases. Desktop `97250598` F7 hunk escalation is not a handoff. No source handoff.
- Head integration onto main; physical cross-host pairing/native; M2b/M4–M11.

Prospective journal scope join C-0414-JOURNAL is ACK'd: only the two named frozen-17424 journals and their empty `.lock` files (no `.tmp` present), plus the two proposal artifacts and `.local/integration` / `.local/tmp` scratch. This worker will keep the private combination current and will not mark the result accepted from tests.
