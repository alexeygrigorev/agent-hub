# Combined conversation platform: early executable integration checkpoint

Task `0414` / `platform-conv-int-0414`. Private inert proposal only. Hub source ownership has not been handed off. This is integration preparation toward M1/M2/M3/P3/public pairing; full M2b/M4–M11 remains required. No main push, no deployment, no production enrollment.

**Disposition: early coherent checkpoint, not accepted.** Independent review of this exact artifact and acknowledged source handoff are still required. Head owns subsequent main/canonical restore.

Author session: `51db3a5a-ab14-4bbc-9be2-183be9d23eda` (`platform-conversation-integration`, engine grok). Verified `aplexer whoami` workspace `/home/alexey/git/agent-hub-wt-platform-conversation-integration`, HEAD `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`.

Portable patch: [CONVERSATION-INTEGRATION.patch](CONVERSATION-INTEGRATION.patch), SHA-256 `83be611ee91b2a21a5fb893922bf0206ce9fb341fd5a4d9d5c0da6efac59959c`. `git apply --check` on a fresh main archive succeeds. 76 paths (61 new, 0 deleted). Tracked `src/` `host/` `public/` `test/` `config/` in this worktree remain byte-identical to main.

## Pin status (do not treat passing tests as acceptance)

| Input | SHA | Status |
| --- | --- | --- |
| Hub main | `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24` | accepted M6 |
| Transport | `d9b4cde07e3718d77b2db9308cd323a5c8e003ed` | scoped Argon `8ecac25e021641781f01c18d662d130812797eac` |
| API | `7c004676dc9c0c6496656cb6a4bc62dbadff09ae` | **REVISE** independent `c10d2038e20e7d72e8abc6a802b5641865d7dc40` (383 cases, 379 PASS / 4 FAIL). Outer-transaction gap in composed `runFairRetentionPass` and HTTP clock fixture. **Provisional; rejected for release.** Head will supply a repaired SHA. Dirty author files are not imported. |
| F7 registry | `ddaf80d82022736da82ff11d521c6a9f92ff593e` patch SHA-256 `a3b2c2fdcc0feb6287e8d8695edd19137687ffe82967c66cc25105491e049b16` | Argon ACCEPT `426fdf7` |
| P3 gap | `0896ebe157cc732342b17fb40a6cf6e096ef93a2` | scoped ACCEPT `f19353e8c9a900f2697f6edeb7da08691cf901e8`. Frozen 17424 contract. Default HOLD. No automatic `max(server read)`. |
| Public pairing | `1149f65394611d05c1f55fcec4e803f105930c8d` patch SHA-256 `1a9e94ef33f25410e3e9834517bb85154cad5afbc5e94eb13e371ca9d28a6947` | **PROVISIONAL**; independent Argon `252a3a83` still running at checkpoint time |

Final base must wait for a committed repaired API pin plus independent acceptance, and for the pairing verdict. This checkpoint keeps a faithful combination of the current pins so that rebase is mechanical.

## Reconstruction (private tree; do not apply to tracked source)

From a checkout that contains the Git objects and already-installed dependencies. Symlink `node_modules`; do not install.

```bash
set -eu
repo=$PWD
base=5dcdf7164b28bd0e3a1d717ac9090c054eca3f24
transport=d9b4cde07e3718d77b2db9308cd323a5c8e003ed
api=7c004676dc9c0c6496656cb6a4bc62dbadff09ae
api_old=da38cb2a00c092423d0b67419b0ea45d6a09dfa9
f7=ddaf80d82022736da82ff11d521c6a9f92ff593e
p3=0896ebe157cc732342b17fb40a6cf6e096ef93a2
pairing=1149f65394611d05c1f55fcec4e803f105930c8d
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
# 3-way: ancestor da38/main, ours F7+pairing tree, theirs API 7c004.
git show "$api_old:src/conversations.ts" > .local/integration/conversations.ancestor
git show "$api:src/conversations.ts" > .local/integration/conversations.api
git merge-file -L F7-applied -L ancestor -L API-7c004 \
  "$scratch/src/conversations.ts" .local/integration/conversations.ancestor .local/integration/conversations.api
git show "$base:src/bus.ts" > .local/integration/bus.ancestor
git show "$api:src/bus.ts" > .local/integration/bus.api
git merge-file -L F7-applied -L ancestor -L API-7c004 \
  "$scratch/src/bus.ts" .local/integration/bus.ancestor .local/integration/bus.api
for path in src/conversation-hub.ts docs/CONVERSATIONS.md test/m3-http.test.ts test/conversation-retention.test.ts; do
  git show "$api:$path" > "$scratch/$path"
done
# Harness hunks in this patch replace SQL-seeded agent tokens with public device issuance
# and bind native qualify to the reconstructing session's real UUID/tag/workspace.
deps=$(realpath "$(git rev-parse --git-common-dir)/../node_modules")
ln -s "$deps" "$scratch/node_modules"
cd "$scratch"
export UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1 TMPDIR="$scratch/.local/tmp"
taskset -c 0 timeout 120 npm run typecheck
```

Alternatively apply [CONVERSATION-INTEGRATION.patch](CONVERSATION-INTEGRATION.patch) to a fresh main archive (`git apply --check` then `git apply`). That reproduces the same private tree, including harness hunks.

When a repaired API SHA lands, repeat the 3-way step with that blob in place of `7c004` for `src/conversations.ts`, `src/bus.ts`, `src/conversation-hub.ts`, `docs/CONVERSATIONS.md`, `test/m3-http.test.ts`, and `test/conversation-retention.test.ts`.

## Overlaps and resolutions

Transport `d9b4` is main plus 49 conversation files (42 reviewed UI/CLI/API/dakit/docs/host files plus 7 integration files: `src/worker.ts`, `docs/CONVERSATION-PLATFORM.md`, `test/conversation-platform-worker.ts`, `test/conversation-platform.mjs`, `test/conversation-platform.test.ts`, `test/vitest.conversation-platform.config.ts`, `test/wrangler.conversation-platform.jsonc`). Its six original API files match `da38`. Bridge files match frozen `17424` / `619ba499`.

1. **API vs transport conversations/bus.** 7c004 supersedes `src/conversations.ts`, `src/conversation-hub.ts`, `docs/CONVERSATIONS.md`, `src/bus.ts`, `test/m3-http.test.ts`, and adds `test/conversation-retention.test.ts`. Domain/integration/security tests are byte-identical to `da38`. `src/worker.ts` keeps the transport export: `ConversationHub` as public `Hub` under existing `HUB` / `idFromName('hub')` / SQLite. 7c004 still exports `Hub as ConversationHub`.

2. **F7 vs 7c004 `src/conversations.ts`.** F7 only rebinds agent-name validation and credential adapters onto `agent_registry`. 7c004 changes retention, unread/gaps, and preview hygiene. `git merge-file` exit 0. All 9 F7 added lines and all 190 7c004 added lines are present.

3. **F7 vs 7c004 `src/bus.ts`.** F7 adds canonical registry addressing, routing-client kind, and provenance ACL. 7c004 adds `RETENTION_CLEANUP_CURSOR_SQL`, `runFairRetentionPass`, and shared-budget cleanup. `git merge-file` exit 0. All 55 F7 added lines and all 179 7c004 added lines are present.

4. **P3 vs transport native.** `d9b4` bridge/cli files equal `17424`. P3 final files overlay directly: explicit `hub bridge-resume`, proof-before-gap-advance, atomic local audit/cursor only, Windows/unrelated-command lazy imports. Reviewed native source is otherwise unchanged. Default HOLD remains. Remote read watermark cannot skip unresolved native effects.

5. **Pairing vs F7.** Pairing patch applies cleanly after F7. Output hashes match the pairing manifest for `src/agent_registry.ts`, `src/claim_admission.ts`, `src/hub.ts`, and the three pairing test files. Exact credential/registry machine binding including NULL, admin existing-machine selection, NULL→machine revokes old unbound credentials, different machine 409. Ordinary machine-bound agents gain no host/root/fleet privilege.

6. **Harness (this checkpoint only).** `test/conversation-platform-worker.ts` seeds a human admin and enrolled machine; it never inserts an agent credential. `test/conversation-platform.mjs` issues the agent token through public `/api/device/code` + admin CSRF approve with `machine_id` + `/api/device/token`. Native identity is this session (`51db3a5a…`, tag `platform-conversation-integration`, engine `grok`, workspace this worktree). Frozen 17424 journals still land at `workspace/.local/conversation-bridge-*.json` (gitignored in this owned worktree). First native probe used `cint-local` and stopped after a workspace assertion; that journal is preserved. Independent fixture `--project=cint-local-r2` completed inbox/reply/reconnect. No automatic `max(server read)`.

No F7 migration/credential/namespace/M6 fix was dropped. No original assertion was weakened to combine.

## Characterizing verification (not acceptance)

CPU 0, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, TMPDIR under owned `.local`. Existing Playwright via `PLAYWRIGHT_PACKAGE`. Symlinked `node_modules`. Peak scratch about 22 MiB. Tracked source hashes unchanged.

| Check | Result |
| --- | --- |
| Typecheck | PASS |
| F7 R2 + F7 local + conversations domain/integration/security + pairing | 79/79 |
| Inherited + all 61 M6 + m3-http + fleet/liveness/root/routing | 254/254 |
| Host conversation CLI + 17424 bridge + P3 gap | 74/74 |
| API retention suite on combined tree | 17/17 (still does not clear independent REVISE of 7c004) |
| Production Hub composition (real DO SQLite, `HUB` name, RPC/cron/alarm identity) | 2/2 |
| Static official dakit lock/borders/colors/scale/CSP | PASS |
| Real Miniflare Worker + official dakit browser + Python CLI + public device-issued token | PASS (7 checks; unread after interior prune is 1 matching retained unread) |
| Genuine own native inbox/reply/reconnect (`cint-local-r2`) | PASS; second physical host untested |
| Mock-API UI desktop/narrow/theme suite | 28/29; one in-flight account-switch attach assertion failed. Real Worker UI path above passed. |

7c004 REVISE defects remain: composed hub fairness cursor is not in one outer transaction with child deletion, and the HTTP clock fixture mismatch is an API author repair. This combination is not a substitute for that repair. Pairing remains provisional until `252a3a83` completes.

## Combined hashes for merge products

SHA-256 of the private tree after reconstruction.

| Path | SHA-256 | Origin |
| --- | --- | --- |
| `src/conversations.ts` | `2448644aa7462e1a09c73890014989850242cbefd79e9cad841d4e866c6c3147` | 3-way F7 + 7c004 |
| `src/bus.ts` | `6300681ab61a275bfccc18912803c2b96dcfd484dbd66c6e048dc12f54d626f5` | 3-way F7 + 7c004 |
| `src/conversation-hub.ts` | `320f479e4d5733cd0fc09bec4529d4fde3bfc66b875516e8fa8916e37968c0e8` | API 7c004 |
| `src/worker.ts` | `f0b067647397e4bd7f263e868787783d6b61d4bfe21d07eb3b9b1fac36ab1adc` | transport |
| `src/agent_registry.ts` | `5aa252d73029b51f4852a7850a02cf24d4b197b4a2e235a366862f99a49c0a3e` | pairing |
| `src/hub.ts` | `05047a9ab5393daa7aee5ea02fdb6923c41c1ed35eddc4a022197600dbb7d6b7` | pairing |
| `src/claim_admission.ts` | `5b6b728de08f32784f03c9a4123356b25fce2425a886a83f2be392b4e63b3dc5` | pairing |
| `host/agent_hub_host/cli.py` | `7a5bda4887e8f93896c941bf5b220759eb1aace4dfdc1e33031c381f1eb11c32` | P3 |
| `host/agent_hub_host/conversation_bridge.py` | `7e7ebaec3148b4cb5522bdead67e171f9adbcbb8e6c0d94085813c80ee313a4e` | P3 |
| `test/conversation-platform.mjs` | `c53cab3e19e2c7f86c72587fdb9a3d5cb80f668bd117fb7adfeb6ef450ecad33` | harness hunk |
| `test/conversation-platform-worker.ts` | `b063ff38ed83d26445f24d34ff6ff559df77b48b4e1d35ac64109f01fb3bad5b` | harness hunk |

## Remaining gates

- Repaired API pin after independent acceptance; rebase 3-way files; never import dirty author trees.
- Pairing independent verdict on `1149f65`.
- Distinct-model review of this exact proposal SHA.
- Acknowledged Hub/registry/M6/test owner leases. Desktop `97250598` F7 hunk escalation is not a handoff.
- Head integration onto main; physical cross-host pairing/native; M2b/M4–M11.

Request: send the next accepted API SHA when it is committed. This worker will keep the private combination current and will not mark the result accepted from tests.
