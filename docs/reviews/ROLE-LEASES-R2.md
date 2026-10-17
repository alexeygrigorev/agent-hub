# M7 Role Leases R2 — Independent Review

- **Verdict**: `SCOPED ACCEPT`
- **Review line**: `Review: approved sha=275c84a39766df45c1028f04bc6f7ba8b65c3287 reviewer=platform-role-leases-r2-review/grok-4.6`
- **Reviewed commit**: `275c84a39766df45c1028f04bc6f7ba8b65c3287` (`test: cover unresolved writer-date fences after expiry [skip ci]`)
- **Direct parent**: `6a2cbd11b16f1e97ca65326c3129f6fc39b95046` (`fix: fence unresolved writer dates across role custody [skip ci]`)
- **Repair merge-base**: `7fd60976de662640c45dfa7146e30859d5743eaa` (merge of `10c65ff` + original review `9574242`)
- **Original M7 source**: `abd48b8461a86f577fc6ae8b5c525d9025376558`
- **Original review record**: `9574242d867f92e615f159a60857b715d75dcb7b` (immutable in this pin; blob `858e177ad6553c5bba3dea8052c5f2d8d2f97c92`)
- **Observed origin/main**: `6cf0c185495a33cf04ec2b986c3c901ed3de2cda` includes M5, which is absent from this candidate. This is not a composed review.
- **Author**: `platform-role-leases-r2/gpt-6` (Codex session `3b36ac3f`)
- **Reviewer**: `platform-role-leases-r2-review/grok-4.6` (session `b8ffcce6-7bce-455d-b8c5-536a5c89ec0d`)
- **Scope**: source fencing of exact `275c84a` only. Main integration, live runtime, physical cross-host adoption, M8a and M8b remain separate. Host writer-run remains an unsupported backend.

This record is SCOPED ACCEPT of exact `275c84a39766df45c1028f04bc6f7ba8b65c3287`. It is not source acceptance of a main composition, not live runtime qualification, and not physical adoption.

---

## Mechanical pins

Verified from Git objects in this worktree. No full SHA inferred from a UUID or an unsupported API.

| Check | Result |
|---|---|
| HEAD | `275c84a39766df45c1028f04bc6f7ba8b65c3287` |
| Direct parent | `6a2cbd11b16f1e97ca65326c3129f6fc39b95046` |
| Merge-base `7fd6097` parents | `10c65ffe38099d85f4ffc530dabc197e8521dde2` `9574242d867f92e615f159a60857b715d75dcb7b` |
| Original M7 | `abd48b8461a86f577fc6ae8b5c525d9025376558` |
| Original report blob vs `9574242` | identical `858e177ad6553c5bba3dea8052c5f2d8d2f97c92` |
| Author trailers | `Agent:platform-role-leases-r2/gpt-6` on both `6a2cbd1` and `275c84a` |
| `git diff --check 7fd6097..275c84a` | exit 0 |
| Host `jobs.py` / `test_jobs.py` vs `abd48b8` | byte-identical |
| `src/role_leases.ts` / `src/hub.ts` vs `abd48b8` | byte-identical |

Changed vs merge-base `7fd6097` (the repair under review):

- `src/jobs.ts`
- `docs/ROLE-LEASES.md`
- `test/role-leases.test.ts`

`275c84a` itself changes only the contract doc and tests; the SQL fence lives in parent `6a2cbd1`. The exact pin includes both. `package.json` / `package-lock.json` match the existing main-tree install. Declared untracked `node_modules` symlink to `/home/alexey/git/agent-hub/node_modules`; symlink only, no install or copy.

---

## Original F1

`docs/reviews/ROLE-LEASES.md` at `9574242` requested High: after a first writer-run intent is recorded and the `writer:${date}` lease expires (121s), a second configured project for the **same UTC date** could claim and `commitRoleExecution` would record a second `execute: true`. HOLD matched full canonical `{"date","project"}`, so distinct projects missed it.

Required fix: fence unresolved writer-run intents by payload date (and legacy `writer-run:YYYY-MM-DD` keys) at **claim** and **effect**, inside the SQL transaction; retain first intent bytes; allow a **different** date to proceed.

---

## What the repair does

`JobsService.hasUnresolvedWriterIntent` (call only inside the claim/effect transaction):

- Applies to `kind === 'writer-run'`.
- Identity is `getLeaseNameForJob({ ...job, role_name: null })` → `writer:${date}`. Date comes from payload, else a `writer-run:` idempotency key. Project, role name, request key and epoch are not identity.
- Matches any other row with `role_effect IS NOT NULL AND role_finish IS NULL` whose SQL `json_extract(payload,'$.date')` or legacy key date equals that identity.

Call sites:

- `claimJob`: refuse before minting a credential (`writer_date_busy` internally). Live `writer:${date}` holders still use snapshot `nowMs` (the original `rawNow()` residual in that comparison is repaired).
- `commitRoleExecution`: `HOLD` / `previous_execution_unresolved` before recording another intent. The existing exact canonical-payload HOLD remains for other same-payload replacements.

Public HTTP `POST /api/jobs/:id/claim` still serializes every `conflict` as `{ error: 'conflict' }` with no `reason`. That mapping is unchanged vs `abd48b8`. Fencing is the 409 and the absent job token, not the reason string.

---

## Independent reproduction

Private probes in `.local/review/probes.test.ts` (fixture pattern taken from the original reviewer probes, originals not modified). Miniflare HTTP + real Hub SQLite. Vitest `--configLoader runner`.

| Probe | Result |
|---|---|
| Two projects, same UTC date, first `intent_recorded`, +121000 ms, JobsService reconstructed from retained SQLite, second claim | HTTP 409 `{error: conflict}`, no `job_token`, one job credential, one `role_effect`, first `role_effect`/`payload`/`role_finish` bytes unchanged |
| Previously issued same-date credential at effect time | both concurrent executes `HOLD` / `previous_execution_unresolved`; current `role_effect` null; retained first bytes |
| Different UTC date after expiry | second claim 200, `execute: true`; first unresolved bytes retained |
| Same-date alias, disable/regrant new key, legacy `writer-run:2099-05-06` with `{}` payload | all claims 409; first intent retained |
| Zero-scope job token | `scopes=[]`, actor `machine:host-a`; cannot jobs-create / root / takeover / bus / claims / conversations / admin role PUT / claim-as-job; extra execute body 400; own execute still `true` |
| Claim credential SQL ABORT and execute intent SQL ABORT | no leaked lease holder, no leaked intent; retry then succeeds |
| Writer lease would expire during credential hashing | snapshot `nowMs` keeps `writer_date_busy`; no second token |
| Two projects, **different** dates, concurrent claims | `[200, 200]`, two credentials, both execute true (no coarse mutex) |

Author `test/role-leases.test.ts`: 30 passed (original 23 + 7 new). Identity filter: 2 passed, 16 skipped. `tsc --noEmit` 0. Host suite not re-run: host bytes unchanged from `abd48b8`; previous review independently recorded 56/56 including 9 `TestM7RoleConsumer`. Production `RuntimeManager.execute_job('writer-run')` still raises `NotImplementedError`; unsupported consumers HOLD before claim/process. That is not M8b qualification.

---

## Author claim audit

From author `.local/roles-r2/evidence-index.json` and retained logs, not invented:

| Claim | Independent result |
|---|---|
| Final 30 role cases (23+7) | This review: 30/30 passed (`.local/review/1792217437517247807`) |
| 2 identity, 16 skipped | This review: 2 passed / 16 skipped (`.local/review/1792217358467668389`) |
| Final tsc / diff-check | tsc exit 0; `git diff --check` exit 0 |
| First role suite early guard 143 | Author record `1792216510961517920`: exit 143, `guard: early_scratch_guard`, allocated 14 893 056, apparent 14 832 253 (14 MiB early, 20 MiB hard unchanged). Head granted 18 MiB early (`scratch_approved_early_bytes` 18 874 368) |
| 4 fixture failures then pass | Author `1792216591284659934`: 26 passed / 4 failed, exit 1. Failures were expectation codes/counts (`401` vs `409`; credential counts including fixture seed). Source `jobs.ts` was already `6a2cbd1`. Next author run 30/30. Fixture-only |
| Host 56 | Not repeated. Host contract byte-identical to previous accepted original; still unsupported backend / HOLD before claim |

No source assertions were weakened and no digests were re-pinned to pass failures.

---

## Commands and results

Serial, `taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `umask 077`, own `TMPDIR` under `workspace.local/tmp`, XDG/npm/vitest caches under `.local/tmp`, Vitest `--configLoader runner`. Wrapper `.local/review/run.py`. Early 18 MiB / hard 20 MiB.

| Record | Command | Exit | Tests | Allocated peak | Apparent peak | Inodes | cgroup | RSS | Tasks |
|---|---|---|---|---|---|---|---|---|---|
| 1792217353542584562 | `tsc --noEmit` | 0 | — | 77824 | 59082 | 18 | 265674752 | 402501632 | 61 |
| 1792217358467668389 | vitest m4 identity filter | 0 | 2 passed / 16 skipped | 749568 | 711502 | 38 | 494239744 | 725749760 | 83 |
| 1792217366385267892 | vitest probes | 1 | 1 failed / 7 passed | 5005312 | 4948271 | 63 | 535764992 | 763383808 | 83 |
| 1792217425460723772 | vitest probes (after probe fix) | 0 | 8/8 | 5013504 | 4951785 | 66 | 535060480 | 766791680 | 83 |
| 1792217437517247807 | vitest author role-leases | 0 | 30/30 | 18329600 | 18229083 | 138 | 581763072 | 802390016 | 84 |

Retained failure (not a source defect):

1. Probes `1792217366385267892`: F1 assertion expected HTTP `reason: writer_date_busy`. `src/hub.ts` claim maps every `res.error === 'conflict'` to `{ error: 'conflict' }` and drops `reason`. Probe corrected; rerun 8/8. Claim still 409 with no token; first intent retained.

Review-run peaks: allocated 18 329 600, apparent 18 229 083, cgroup 581 763 072, RSS 802 390 016, tasks 84. Combined owned scratch stayed under the 18 MiB early guard (18 874 368) and 20 MiB hard cap. Guard never tripped. No unknown artifacts or peer worktrees deleted.

---

## Residuals (not High findings on this pin)

- Public claim 409 omits `writer_date_busy`. Pre-existing HTTP mapping. Effect-time HOLD still returns `previous_execution_unresolved`.
- `roleCurrent` / `finishJob` / `configureRole` still read `rawNow()` in places. The F1 claim-time writer-date live-lease comparison now uses snapshot `nowMs`. Clock rollback continues to fail closed.
- Confirmed `role_finish` (including `failed`/`cancelled` finish receipts) is resolved: a later same UTC date may proceed after the role lease is released. Uncertain intents (`role_effect` set, `role_finish` null), including fenced/expired/no-first-action rows, stay and still refuse duplicates. Prune still keeps unresolved rows.
- SQL fencing cannot stop an OS/provider process already started. Host writer-run remains `NotImplementedError` / HOLD-before-process. M8a/M8b stay open.
- This candidate does not contain main `6cf0c18` / M5. Head composes and tests that merge separately.

---

## Limits

- No source, tests, contract, or config edits. No push. No deploys, cloud, DNS, services, timers, enrollment, binaries, Rust, installs, or paid credits.
- No new Claude/fullGLM/provider jobs. No panes. No APLEXER binding override.
- Peers left untouched, including `hub-zai`, `platform`, `platform-links-r2-review` / `platform-links-m5-compose`, `platform-metrics`, and `platform-host-binding`.
- Fake host consumer is not backend qualification.

---

## Verdict

`SCOPED ACCEPT` exact `275c84a39766df45c1028f04bc6f7ba8b65c3287`.

Original F1 is repaired at both claim and effect for same-UTC-date unresolved intents across projects, aliases, new keys, reconfiguration, restart, and legacy dated keys. Different dates proceed. First intent bytes are retained. Host remains an unsupported writer backend.

`Review: approved sha=275c84a39766df45c1028f04bc6f7ba8b65c3287 reviewer=platform-role-leases-r2-review/grok-4.6`
