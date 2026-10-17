# Windows integration delta

Prepared for exact different-model review; not accepted, applied or integrated. CFB remains provisional while its independent Argon review is pending. Physical Windows DLL/ABI/NTFS and M5/M11 remain OPEN.

This executable delta composes the independently scoped-reviewed Windows host port into frozen conversation assembly `cfb4dc26985cbe39a1ae770ade4a6a57880edcbc`, based on main `5dcdf7164b28bd0e3a1d717ac9090c054eca3f24`. Windows input is `2de0a6c1846b110227cd1d15e99a7af6999158f5`, scoped source ACCEPT `3d0dfadbed35812aeaacbb98f304acfecf711e91`. Those component verdicts do not accept this composition.

Patch SHA-256: `bf2f3df7070c2c9145ca86dd4ada7e5ff6399c6e478284807e0d1009b5d61500`; exactly the reviewed Windows patch, with no adaptation. CFB patch SHA-256: `e8c91a9c3f3a795360c85eec300d3df9187a361d57f9a9dfc9312ad60e673d10`. Independently verified six existing input hashes and four NEW absences before private application. All 193 other CFB paths retain exact bytes; no API/auth/UI/F7/pairing/M6 or CLI behavior edit, M5/M8 import, or peer dirty input.

Full manifest algorithm: sorted `[path,sha256]` rows encoded as compact JSON, SHA-256 of UTF-8 bytes; exclude runtime/dependency/build output. Input:199 files, `17aab235373ff0f98cce9ff361b74333d9d3bd23bf45790d471865f3ea02223a`. Output:203 files, `5fb8eeace9f7b936e1b9043902cea3783cd0aa72decabedb51f96bef298e8804`.

Private reconstruction and manifests are retained under ignored `.local/windows/`. Only this report and adjacent delta are committed. The verification below runs the exact frozen composition; it is author evidence pending independent exact-version review. Product source exists only in private Git-object reconstruction. Head owns Agent Branches fork/WIP/main, source leases, independent review, integration and private backup. No push/deploy/source application.

## Exact future owner scope and hashes

| Path | CFB input SHA-256 | Composed output SHA-256 |
| --- | --- | --- |
| `host/agent_hub_host/bridge.py` | `2b5e775dbbf194fcb619171a483b9b4152af8a2e76c46a6450b47ead8f9eb464` | `033db99502033296143522917d01bcf63073f41cd0e3bf4f32227187a017b5ea` |
| `host/agent_hub_host/config.py` | `2ee1abe6c62cf93d494cb0098525d69750f462cb2ec5024a8b5cacd8a8e29537` | `6cce08e8682e4b70b3d26231716dc1ceb4c041f618fd4fb7ecf13212e38999fa` |
| `host/agent_hub_host/conversation_bridge.py` | `7e7ebaec3148b4cb5522bdead67e171f9adbcbb8e6c0d94085813c80ee313a4e` | `747e3cf7ccd2296f89cd833c7a6864dfce7602f79bf50095e6b17cc3297b5855` |
| `host/agent_hub_host/native_bridge.py` | `b8f1f4dbac0093f48176542fad393342866c07348b7728198ec643ab0fce0b3f` | `528dab707ebd7ae12eb97d7db94a6cf17658108dfd1e16f7d1c77e89c5fc8ab9` |
| `host/agent_hub_host/private_fs.py` | `NEW` | `ef4b6436bb74c08b2e4e367f82bf5a02bd1db9e3d04f416b480b46f8c8843a18` |
| `host/agent_hub_host/win32_fs.py` | `NEW` | `34589f69dc0f11cabb79fd08afaf0c4a48b3a7e6ee2c2bf549b7066ade99c302` |
| `host/tests/test_bridge.py` | `783d38efeac5047c0c0182d073ef4e3b553526c3c4c29364c82531ecdecf51e5` | `dcb6dd44cd21753ef9e773df5aefc8ed2b60e30268cbdbff23444bbc73bf237c` |
| `host/tests/test_config.py` | `7d6fe1beab39bb10409f829458e1c7280be78870bb03f74c32b9859dc9940ab8` | `761b35077d66fc2c541de0d03409c693fd1ee65d0b17a85d8f1d1eef830c9399` |
| `host/tests/test_private_fs.py` | `NEW` | `bd56b0cf54df9a92a39430754e6bc15e6a1f7fc1e5cac580bd94dad3529cc1bd` |
| `host/tests/test_windows_imports.py` | `NEW` | `8a7d5bb02994c0558c13843f62823eb9d89c0e3eac5431e138c3e9291b06c52b` |

## Composed verification and retained failures

Linux / CPython3.12.3 / Node24.13.1; every final check below passed. Counts describe separate evidence layers, not a summed acceptance score.

| Check | Result | Retained evidence under `.local/windows/` |
| --- | --- | --- |
| Full host union, including new storage/imports, legacy bus, native contracts, auth, public CLI and P3 |654 cases:653 PASS +1 actual-Windows-only SKIP;749 passing subtests, counted separately |`host-final.log`, `host-final-counts.json` |
| Actual POSIX backend |Real subprocess lock contention, kill/reacquire, atomic state/bounds, hardlinks, unsafe modes, nofollow/FIFO/ancestor-swap and replacement failure PASS within union |`host-final.log`; exact reviewed `test_private_fs.py` retained in both reconstructions |
| Composed Worker registry/F7/pairing/auth/security/conversation regression |92/92 PASS |`worker-composed.log` |
| Compile / TypeScript / official kit |58 host Python files compile; typecheck PASS;21 official locked kit files and both static checks PASS |`compile-tracked.json`, `typecheck.log`, `kit-*.log` |
| Real public Worker/browser/Python CLI preflight |8 PASS groups: signed human/current CSRF, public device login/AUTOcfg, human/CLI reply/read, reconnect/catch-up, retention metadata and stale-member denial |`public-preflight.log`, `public-preflight-browser.png` |
| Public pairing/browser/CLI/refresh |9 PASS groups, including bound and NULL machine responses, config conflict, canonical-identity and concurrent-assignment races, ordinary-agent privilege denial |`public-pairing.log` |
| Genuine own native POSIX / public Worker/browser/CLI / P3 |10 PASS groups, gap-first0mail then1 own envelope/1 own reply/restart dedup;0 unresolved |`native-posix.log`, `native-reconciliation.json`, `tree/.local/path/native-evidence.json` and `native-receipts.json` |
| Local production bundle / actual HUB binding |PASS,552.38KiB upload estimate; `wrangler deploy --dry-run` only |`bundle.log` |
| Fresh main + CFB + delta / actual tracked files |203/203 output hashes match;131 tracked nonartifact files unchanged before/after |`fresh-reconstruction.json`, `freeze-*.json` |

The eight preflight groups recur in the native run and are not added to its ten unique groups. The focused eight mock tests and initial union run are repeats, excluded from final unique case counts. The simulated Win32/CRT cases are source contract evidence only; the actual Windows-only test is skipped on Linux.

First union:654 cases,649 PASS/4 mock HTTP CLI errors/1 Windows SKIP. Full failing log is retained in `host-union.log`. My runner's global `HUB_CONFIG_FILE` overrode the fixture's isolated XDG config, causing `Not logged in` / `No valid credentials`. `mock-original-diagnostic.log` preserves the full subprocess error and0775 fixture-parent precondition. The corrected runner removes that override and sets child umask077; focused8/8 then full653 PASS+1 SKIP. No product/test edit, assertion relaxation or private-permission weakening. `run-check-initial.py`, corrected `run-check.py`, diagnostic probe and all receipts remain. Expected injected failures inside passing negative tests are not test failures. Optional hard-coded external registry importers were not run.

Exact core commands, with the isolation above and `taskset -c 0 timeout 360`:

```bash
python3 -m unittest discover -s host/tests -v
npm run typecheck
npm test -- --config test/registry-f7.config.ts test/existing-ui.test.ts test/machine-pairing.test.ts test/registry-r2.test.ts test/registry-f7.local.ts test/conversations.test.ts test/conversation-security.test.ts test/conversation-integration.test.ts
node test/existing-ui.checks.mjs
node test/conversations-ui.checks.mjs
node test/conversation-platform.mjs --project=cint-local-winpreflight
node test/machine-pairing.mjs
npm run build
# Separate exact-head-ACKed effect gate, genuine author only; never blindly replay:
node ../public-native.mjs --native --project=cint-local-winmerge
```

The retained native journal SHA-256 is `db6ff994ad845fa28fef62ac3cf604e4cdf8bc7af3e49faf3b7a7cfd34abc128`; journal and persistent lock are0600. Settled cursors are main4/gap3,1 known delivery,1 done reply,1 audit covering missing2,0 unresolved. Gap-only native envelope IDs were unchanged before/after all HOLD/skip/audit/replay/catch-up assertions. No native staging leftovers remain; random temporary names were not retained by sampling, so no invented name is reported. Head independently reconciled exact native fullbody/data/kind/uniqueID/from/to/workspace/reply_to against the authoritative own log, journal hash/modes/cursors/audit/zero-pending and absence of staging leftovers; ACK `01a147e8-fbe7-7e32-9efd-e5f322d4e7fc` is retained privately. No additional cycle is authorized.

## Resource and retention bounds

Half-second samples observed 32366592 allocated / 28902134 apparent scratch bytes; both below35MiB (36,700,160 bytes). This is sampled evidence, not a guarantee about unsampled instants. Final measurement before sealing: 21696512 allocated / 18141302 apparent. Dependency symlink targets are excluded; path-based apparent measurements can count shared file inodes more than once. Kernel cgroup peaks were666,951,680 memory bytes and140 tasks, below1500MiB/256. Available RAM38,747,762,688 bytes and disk78,009,999,360 bytes exceeded10GiB/50GiB floors. Head manages the512MiB aggregate reservation.

All owned logs, failed diagnostics, probes, manifests, driver/diff, two small browser images, both source reconstructions and native journal/lock remain retained for head verification. No deletion-before-handoff, dependency/cache/history copy, new agent/provider job, install, cloud operation, deployment, enrollment, service/timer/global/native-binary change, public post or push occurred.

## Reproduce and inspect

Head task `task-0420`, Agent Branches agent `platform-win-merge-0420`; head owns all fork/WIP/main custody. Author is genuine native `platform-win-merge`, UUID `4a0b74cb-7b0e-4092-aa8a-20af4da7f3ae`, Codex/GPT-6 runtime family (no backend minor exposed). Early coherent two-artifact checkpoint: `981430f4793160e4580789889e13d5c29e0a824f`. No source owner lease, push or integration is implied.

Reconstruct from immutable objects into a NEW owned private scratch directory. Do not apply this patch to main: its input is the exact CFB199 assembly. Stop repository discovery at the scratch parent so `git apply` cannot silently skip nested paths. Verify every input/output hash, not just exit status.

```bash
# Set candidate to a NEW owner-approved scratch path under this task's .local/windows.
mkdir -m 700 "$candidate"
git archive 5dcdf7164b28bd0e3a1d717ac9090c054eca3f24 | tar -x -C "$candidate"
git show cfb4dc26985cbe39a1ae770ade4a6a57880edcbc:docs/proposals/CONVERSATION-INTEGRATION.patch > "$candidate/../cfb.patch"
cd "$candidate"
GIT_CEILING_DIRECTORIES="$(dirname "$candidate")" git apply --check ../cfb.patch
GIT_CEILING_DIRECTORIES="$(dirname "$candidate")" git apply ../cfb.patch
# Check all199 input hashes/digest, then apply the adjacent Windows delta similarly.
GIT_CEILING_DIRECTORIES="$(dirname "$candidate")" git apply --check "$delta"
GIT_CEILING_DIRECTORIES="$(dirname "$candidate")" git apply "$delta"
# Check all203 output hashes/digest and unchanged193 CFB paths.
```

The retained executable `reconstruct.py` does this and verifies every output against `output-manifest.json`. Its second fresh reconstruction matched203/203. Input/output/changed manifests and `integrity.json` are retained under ignored `.local/windows/`. Manifest digest implementation:

```python
rows = [[str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()]
        for p in sorted(root.rglob('*')) if source_file(p)]
digest = hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()
# source_file excludes symlinks and .local/node_modules/dist/.wrangler output.
```

Reuse existing dependencies only by symlink: `node_modules` resolves to the existing hub installation; browser resolution uses `PLAYWRIGHT_PACKAGE=/home/alexey/git/dataops/package.json`; shared `dtc_device_login` is read from existing auth source. No install, Rust build, native/global binary or global config change. Commands run from the reconstructed tree with `UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 GOMAXPROCS=1`, absolute owned `TMPDIR=$candidate/.local/tmp`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, isolated XDG config and cleared inherited Hub credential variables. Do not set a global `HUB_CONFIG_FILE` for tests that supply their own XDG fixture. Child umask077 makes ordinary private test parents private; explicit unsafe-mode assertions remain unchanged and run.

Run stages serially on CPU0. Retained `run-check.py` records exact commands, exits, durations and sampled resources; `unittest-count.py` counts cases/subtests/skips separately. Relevant commands are full `python3 -m unittest discover -s host/tests -v`, `npm run typecheck`, composed Worker security/registry/pairing/conversation tests, the two UI/kit check scripts, public machine-pairing browser checks, and `npm run build` (local `wrangler deploy --dry-run` only). Native execution has its separate gate below; never run the frozen author's default native identity.

## Native source and adoption boundaries

The sole native proof uses the NEW reviewed storage backend and genuine own binding on Linux; real Windows DLL/ABI/NTFS behavior and a physical second host remain UNPROVEN. Exact203 product bytes are frozen before and after effects. Private driver `public-native.mjs`, its SHA-256 and `driver.diff` are retained. Changes from frozen CFB harness are only reconstructed root, genuine own identity/workspace, plus a separately head-ACKed read-only observer that compares own native envelope IDs before/after the gap-only stage. Every original assertion and ordering remains.

Head explicitly ACKed exact frozen tree/patch/actor/project and journal/lock/tmp paths before effects; supplemental zero-mail observer and parent metadata operations received separate ACKs. Parent `.local` was owner0775 before and owner0700 after the narrowly authorized nonrecursive chmod; no own journal or native effect existed before repair. No permission relaxation or automatic migration. This private-parent precondition is part of adoption. Journal basename is `conversation-bridge-d9f0cdab790714cf495dccb5.json`, with unchanged SHA256(compact JSON `[actor,project]`) rule; actor `cint-local-winmerge:platform-win-merge`, project `cint-local-winmerge`. Journal/lock and any crash staging evidence remain private and unchanged for head reconciliation. No prior identity/journal was read, copied, reused, reset or deleted.

Public positive credentials come only through actual CLI device login + signed local human/current-CSRF approval and automatic authenticated machine persistence. Miniflare uses actual Worker dispatch and HUB SQLite binding; no unsupported non-SQL object or SQL agent-token/manual-machine workaround. Only local human/machine fixtures are seeded. Public pairing checks preserve refresh, originally NULL machine assignment, rebind and privilege refusals. Source/union checks retain strict identity/origin/machine/native binding, pending-send/reply uncertainty, defaultHOLD, authenticated retained-range proof and atomic local audit rules. No fake public membership or Windows always-HOLD fallback is credited.

## Concrete Windows owner handoff; physical gates OPEN

After exact combined acceptance, head hands the ten table paths to their actual source owners and supplies canonical source hashes, genuine existing enrollment/native binding, current public login/membership, human peer and exact journal scope. Missing prerequisites HOLD at that owner; this kit grants no enrollment, credential, remote Windows, desktop, service or deploy action. Win35 root stays stood down.

Existing Windows owner must record CPython3.11+/OS/aplexer/NTFS versions and run the retained stdlib private-file/import kit on fixed local NTFS. Separate simulated contracts from actual DLL/ABI results. Prove pointer/structure/handle widths, current SID/protected owner DACL and creation permissions, native inherited-ACL chain, pinned ancestors, reparse/junction/hardlink/ADS/network/device refusal, bounded reads/writes, subprocess contention/process-death release and failed/atomic replacement. Preserve existing records/ACLs; unknown proof fails closed.

Then use real public CLI automatic cfg/auth/refresh and genuine own native provenance for approved bidirectional human/agent messages, offline/reconnect order, one mapped reply and restart dedup. Perform gap-first zero-mail HOLD/retained-row refusal/missing-range audit/replay/catch-up proof before human effects. Never delete live server history to manufacture a physical gap; real explicit resume requires separate authenticated evidence and head ACK on the same journal. Stop on uncertainty; retain source hashes, IDs, cursors/audit, private logs and exact exits. The detailed bounded owner checklist remains retained in `.local/windows/windows-owner-checklist.md` and the reviewed Windows input report.

Rollback stops only the foreground bridge, retaining journal/lock/receipts and crash stages; never restore older cursors. Head coordinates compatible prior-source restore and independent reconciliation/restore proof. Existing services/timers remain until acknowledged migration. Different-model exact combined review, CFB verdict/integration, source leases, main/private-backup verification, M5 physical follow-up and M11 migration/second-host gates remain required. Source-port delivery does not close M1–M11.
