# Local conversation transport qualification

Candidate only: independent review of this exact integration and head integration are required. No deployment or main push occurred.

## Frozen inputs and composition

Base main: `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`. Frozen assembly: `cee47dff8b423d82eb15e1f9b39cc39ff9ede59f`, containing API `da38cb2a00c092423d0b67419b0ea45d6a09dfa9`, scoped UI `7179868ae9f1723d9070a37f107a3b4f635c5cb6`, scoped CLI/bridge `17424c66be6501a4c763ebfe694679880ff2cca7`, and inherited accepted M6. Other source owners' repairs are not included.

`src/worker.ts` exports the reviewed `ConversationHub` value as public `Hub`. The namespace remains `HUB`, class name `Hub`, ID `idFromName('hub')`, and the existing SQLite migration. Its exported TypeScript `Hub` type and namespace interface retain the inherited base contract so existing typed callers remain compatible. No second namespace, database or authority is introduced. The gated root asset now serves `conversations.html`; fleet remains accessible at `/fleet.html`. All application assets remain the reviewed official dakit assets.

## Reproduce locally

Use existing repository dependencies (a symlink to an existing installation is sufficient) and the existing Playwright installation. Do not install dependencies. From the worktree:

```sh
mkdir -p .local/path .local/tmp
export TMPDIR="$PWD/.local/tmp"
export UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1
taskset -c 0 node test/conversation-platform.mjs
taskset -c 0 node node_modules/vitest/vitest.mjs run --config test/vitest.conversation-platform.config.ts
taskset -c 0 node node_modules/typescript/bin/tsc --noEmit
taskset -c 0 node test/conversations-ui.checks.mjs
PYTHONPATH=host PYTHONDONTWRITEBYTECODE=1 taskset -c 0 python3 -m unittest discover -s host/tests
WRANGLER_SEND_METRICS=false taskset -c 0 node node_modules/wrangler/bin/wrangler.js deploy --dry-run --outdir .local/path/bundle
```

The standalone harness bundles the actual Worker through the installed esbuild, runs real Miniflare/workerd with SQLite and a loopback ephemeral HTTP port, and opens one Chromium context. The `ASSETS` service reads the actual reviewed static files after the production Worker's private-asset gate. There are no conversation API mocks or replacement schemas. `PLAYWRIGHT_PACKAGE` can name an existing package.json containing Playwright; the default is the pre-existing dataops installation.

Only the isolated test entrypoint has a randomly authenticated fixture route. It seeds a local human, machine and agent credential through the real inherited schema and signs a session with the production cookie helper. Agent registration then uses public `/api/agents` with admin session and CSRF. Production `wrangler.jsonc` never references this entrypoint. Public agent device approval currently cannot bind a machine ID; the local credential fixture is not production enrollment or F7 qualification. A second fixture operation backdates one message and invokes the actual retention service for the gap probe.

The actual Python parser is invoked as `python3 -m agent_hub_host.cli conversations …`: participants, history, read, send with stdin body/stable key/reply target, and get. Credentials remain in private environment/config, never argv or committed evidence. The harness has a 180-second deadline, 30-second CLI bounds, browser/Worker cleanup, one browser context and ephemeral storage. Keep scratch below 35 MiB and retain the assigned resource floors.

## Evidence and limits

The native run passed seven transport checkpoints: real auth/CSRF/asset gating; registered CLI identity; human browser create/send and CLI receive/read; CLI reply/idempotency and browser read watermark; offline reconnect/reload without duplicates; genuine native self-inbox/reply/reconciliation; immediate membership-removal denial. It stored four ordered messages and observed 26 browser conversation HTTP requests. Three bridge cycles delivered one human envelope, relayed one native reply as the authenticated actor with the mapped `reply_to`, and produced no duplicate or loop. The browser showed all four messages using the actual Worker API.

The production composition tests execute bound DO RPC, both cron branches and alarm, check the exact exported class and inherited WebSocket/lifecycle method identity, and exercise real auth/health/registry routes. The focused inherited/API run passed 113 tests across nine files: composition, conversation integration/security/domain, fleet assets, auth, token auth, bus and routing. All 611 host tests passed. Typecheck, static dakit/CSP/transport checks and local dry-run bundle passed. Private logs, screenshot, native receipts and machine-readable counts live under `.local/path/`; fixture tokens are not included in public evidence.

For the optional native probe, run only in the genuine `platform-path` session after checking `a whoami` and claiming its exact generated private journal paths:

```sh
taskset -c 0 node test/conversation-platform.mjs --native
# Separately authorized final independent fixture after the first completed run:
taskset -c 0 node test/conversation-platform.mjs --native --project=path-local-final
```

It uses the installed native commands with the session's own tag/UUID, inbox-only send, and a native mapped reply. No APLEXER rebinding, `--from`, pane input or other agent is used. Journals `conversation-bridge-91dcc5822c0f9c8d1c566874.json` (original) and `conversation-bridge-6b06d10f0aee9b2656ad6255.json` (final fixture), each plus `.lock`, remain private under `.local/`. The ephemeral fixture cannot reuse an existing journal with a different origin/store: preflight refuses. Preserve journals and uncertain-send HOLD/reconciliation; never delete a reservation or use a new project to retry an uncertain effect. `--project=path-local-SUFFIX` permits an explicitly authorized independent fixture after a completed run, with separately claimed journal paths. This proves only local own-session transport; a second physical host remains untested.

Full M1 and F7 remain unaccepted. The separate API repair owns shared bus/claim/conversation cleanup budgeting, pruned-gap unread/metadata and malformed version counters. The actual HTTP retention probe retains seq 1 and 3, reads through 1 and returns seq 3; the frozen API reports unread 2 although only one unread row remains. This is recorded as a defect, not asserted as correct. The frozen native bridge requires `msg.seq == cursor + 1` before delivery/read, so this page would HOLD with `History is not contiguous/canonical`. Native execution of that gap is not claimed. Bridge recovery needs an explicit reviewed gap/catch-up contract and durable notice, without fabricated delivery or silent skipping. No API/bridge/registry repair was applied here.

Head must integrate independently reviewed repairs, rerun real transport against the resulting exact source, arrange distinct-model integration review and qualify physical cross-host behavior before acceptance. Exit status alone is not acceptance.
