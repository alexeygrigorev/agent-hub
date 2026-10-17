# M7 Role Leases — Independent Review

- **Verdict**: `REVISE`
- **Review line**: `Review: changes-requested sha=abd48b8461a86f577fc6ae8b5c525d9025376558 reviewer=platform-role-leases-review/grok-4.6`
- **Reviewed commit**: `abd48b8461a86f577fc6ae8b5c525d9025376558` (`feat: enforce role custody through job effects and host consumer [skip ci]`)
- **Direct parent**: `5546a29d4c0224b6770844e18ff09428cf1c55ad`
- **Frozen origin/main at source**: `16d66d69708268ab84089447534f0439e0bfd893`
- **Author**: `platform-role-leases/gpt-6` (Codex session `7566b10d`)
- **Reviewer**: `platform-role-leases-review/grok-4.6` (session `b050da9a-63e5-4e8c-a4db-8ffac378936a`)
- **Scope**: source fencing of exact `abd48b8` only. Main integration, live runtime, physical cross-host adoption, M8a and M8b remain separate. Author does not claim M8a/M8b; this review does not either.

This record is SCOPED REVISE of exact `abd48b8461a86f577fc6ae8b5c525d9025376558`. It is not source acceptance, not main integration, and not runtime qualification.

---

## Mechanical pins

Verified from Git objects in this worktree against the head receipt at owner-relative `.local/platform/role-leases-source-manifest.json` (agent-coordination). Compact JSON of the 206 `[path, sha256(blob)]` pairs:

`4a2e920d23e33bb1c0605ce7fd0087f266aa61d81e3082c5ddd38ed11b211943`

| Check | Result |
|---|---|
| HEAD | `abd48b8461a86f577fc6ae8b5c525d9025376558` |
| Parent | `5546a29d4c0224b6770844e18ff09428cf1c55ad` |
| Frozen origin/main | `16d66d69708268ab84089447534f0439e0bfd893` |
| Manifest pairs | 206, mismatches 0, missing 0 |
| Recomputed compact digest | match |
| Changed vs frozen 16 | exactly 7 paths |
| Other base source paths | 199 unchanged vs 16 |
| `git diff --check 16..abd` | exit 0 |
| Author trailer | `Agent:platform-role-leases/gpt-6` |

Seven paths vs frozen `16d66d6`:

- `docs/ROLE-LEASES.md` (new)
- `host/agent_hub_host/jobs.py`
- `host/tests/test_jobs.py`
- `src/hub.ts`
- `src/jobs.ts`
- `src/role_leases.ts` (new)
- `test/role-leases.test.ts` (new)

HEAD also tracks six extra proposal/review files already present on the frozen base lineage and outside the 206-file source manifest. They are unchanged by this candidate. Live `origin/main` later moved independently to `10c65ffe38099d85f4ffc530dabc197e8521dde2` (post-main Argon record). This review pins the source candidate against frozen `16d66d6`, not that later main.

Declared `node_modules` symlink to the existing main-tree install (same `package.json` / `package-lock.json` hashes). Symlink only; no install, copy, or stage.

---

## Finding F1 — High — second writer-run execution intent after lease expiry

**Severity**: High  
**Location**: `src/jobs.ts` `commitRoleExecution` unresolved-intent query (exact `kind` + full canonical `payload`); `claimJob` writer-date exclusion (active unexpired `writer:${date}` lease only). Canonical payload is built as `canonical({ date, project })` in `offerRoleJob`.  
**Author suite gap**: `test/role-leases.test.ts` covers claim-time `writer:${date}` mutual exclusion and same-payload HOLD after takeover. It uses a *different date* (`2099-05-07`) as “different approved work”. It does not execute a second project for the *same* UTC date after the first intent is unresolved and the lease has expired.

### What holds

- Two role names in one project, same writer date, concurrent claim: HTTP `[200, 409]` and exactly one job credential.
- Two projects, same writer date, concurrent claim: HTTP `[200, 409]` and exactly one job credential.
- Same project, role alias, same canonical payload after takeover: `execute: false`, `status: HOLD`, `reason: previous_execution_unresolved`; old `role_effect` kept, new `role_effect` null.
- Legacy `writer-run` SQL job and role job: claim-time exclusion, one writer credential.

### What fails

Writer-date exclusion is claim-time and lease-lifetime only. After the first holder records intent and the `writer:${date}` lease expires, a second configured project for the same UTC date can claim and `commitRoleExecution` records a second `execute: true` / `intent_recorded` row. The first unresolved `role_effect` is retained. Competing writers therefore get two execution intents.

Cause: unresolved HOLD matches the full canonical JSON `{"date":"...","project":"..."}`. Distinct projects are distinct payloads, so HOLD does not fire. `writer:${date}` is shared at claim only while that lease is live.

This contradicts the required global writer-date serialization: two role names/projects/different keys on the same writer date must yield one credential *and* one execution intent for competing writers. Different *dates* may proceed without dropping old uncertainty; a second project on the same date is competing writer work, not different approved work.

Host production still HOLDs before claim/process because the writer backend raises `NotImplementedError`. The SQL grant is still a second authorized intent. A later M8b consumer that honors `execute: true` would start a second writer-run for that date.

### Repro

Independent Miniflare HTTP + real Hub SQLite (review-owned `.local/review/probes.test.ts`, `--configLoader runner`). Record `.local/review/1792214896811536394`.

1. Admin CSRF `PUT` enables `demo/writer` and `other/writer` with the same hosts and `writer-run`/`execute`.
2. Offer/claim/execute `demo/writer` for `payload.date=2099-05-06` → `{execute:true,status:intent_recorded}`.
3. Advance 121000 ms (TTL 120s) so the writer-date lease expires.
4. Offer/claim `other/writer` same date on `host-b` at the live role epoch → claim `200`.
5. Execute with the new job token and epoch.

Observed: `{execute:true,status:intent_recorded}` while `jobs.role_effect` for the first id remains set and `role_finish` is null. Assertion expected HOLD; 8 other independent probes in that file passed.

Author `test/role-leases.test.ts` 23/23 still passes: the gap is untested, not a failing author assertion.

### Fix

Keep claim-time `writer:${date}` exclusion. Also fence unresolved writer-run intents by **payload date**, independent of project/role key/epoch:

- In `commitRoleExecution`, HOLD `previous_execution_unresolved` when any other `writer-run` row has `role_effect` set, `role_finish` null, and the same `$.date` (do not require exact payload JSON).
- In `claimJob`, refuse `writer_date_busy` (or a dedicated unresolved reason) when such an unresolved same-date intent exists, even if the `writer:${date}` lease has expired.
- Add an HTTP+SQLite case for two projects, same date, unresolved first intent, expired lease → second claim/execute HOLD, first intent retained, one `role_effect`.
- Keep the existing different-date takeover path (`2099-05-07`) as different approved work.

Do not re-pin digests or weaken assertions to hide the second grant.

---

## What independently held

Auth/config/epoch (public HTTP):

- Non-admin configure 403; mixed path encoding (`Demo`, `demo%2fextra`) 400; reserved namespaces refused by `roleName`.
- Stale generation → `role_unauthorized`; stale/omitted epoch → `role_conflict` / `invalid_role_job`.
- Extra payload keys / unsafe URL / expired dated idempotency key → `invalid_role_job` / `idempotency_key_expired`.
- Current holder renew + finish 200; host token, omitted epoch, disable, credential rotation refuse (≥400).
- Zero-scope job token (`scopes=[]`, actor `machine:host-a`) cannot create jobs, read root, takeover, bus send, claims, fleet, conversations, or reconfigure roles.
- Disable/regrant fences the old idempotency key; new key offers; SQL ABORT on execute rolls `role_effect` back to null; concurrent retry records one true grant and one HOLD; old token then ≥400.

Host effect contract (stdlib, no provider/native/timer):

- Production `RuntimeManager.execute_job('writer-run', ...)` raises `NotImplementedError` before a process.
- Unsupported/unqualified consumer HOLDs before claim and before execute intent.
- One qualified fake effect; lost/uncertain grant is not retried; inventory/`MagicMock` is not qualification.
- Execute HTTP client does not retry a lost response.

Retention/caps (author cases, source read):

- `ROLE_CAPACITY=64`, `ROLE_JOB_CAPACITY=1024`, prune LIMIT 64.
- Abandoned offered rows expire on admission; unresolved `role_effect` with null `role_finish` is not deleted; dated keys rejected after UTC day.
- Capacity counts remaining role jobs including unresolved intents (fail closed; no M5R2-style pretend retirement).

SQL: `commitRoleExecution` authorization + intent write share one `transactionSync`. Host cannot turn a fake consumer into a verified provider.

---

## Author claim audit

| Claim | Independent result |
|---|---|
| Final 23 role cases | `test/role-leases.test.ts`: 11 named `it` + 12 mutation `it` = 23. Review run 23 passed (`.local/review/1792214967904405203`). |
| 2 writer-identity cases | `test/m4-jobs.test.ts -t 'lease identities\|createJob permissions'`: 2 passed, 16 skipped (`.local/review/1792214989938699450`). Author “39 skipped” is the same file run with a broader include, not 39 extra unique cases. Skips are not passes. |
| 56 host | `host/tests/test_jobs.py` 56 passed (`.local/review/1792214999805502916`), including 9 `TestM7RoleConsumer`. |
| Typecheck / diff-check | `tsc --noEmit` exit 0 (`.local/review/1792214673312467660`); `git diff --check` exit 0. |
| Prior 30 root/jobs, 7 M6, 4 subset | Audited in author evidence-index only. Not re-run (no broad 206/453/635/89 PNG repeat). |
| Compact digest / 7 paths / 199 unchanged | Match, see pins. |
| Author peak 12 140 544 allocated / 11 995 951 apparent | Same order. This review’s author-23 run measured 14 036 992 / 13 920 980 on a separate vite cache. |
| TS2589 / credential-count fixture / monitor journal race | Consistent with retained author failure records; runtime assertions not weakened on `abd48b8`. |

---

## Commands and results

Serial, `taskset -c 0`, `UV_THREADPOOL_SIZE=1`, `NODE_OPTIONS=--v8-pool-size=1`, `GOMAXPROCS=1`, `umask 077`, own `TMPDIR` under `workspace.local/tmp`, Vitest `--configLoader runner`. Wrapper `.local/review/run.py`.

| Record | Command | Exit | Tests | Allocated peak | Apparent peak | Inodes | cgroup | RSS | Tasks |
|---|---|---|---|---|---|---|---|---|---|
| 1792214673312467660 | `tsc --noEmit` | 0 | — | 61440 | 42328 | 13 | 303136768 | 433848320 | 79 |
| 1792214679325813239 | unittest `-k TestM7RoleConsumer` | 0 | 9/9 | 69632 | 45090 | 15 | 114900992 | 242155520 | 77 |
| 1792214688534855551 | `python3 .local/review/host_probes.py` | 1 | 4 ok + 1 error | 77824 | 47052 | 17 | 116428800 | 243916800 | 74 |
| 1792214699036073676 | same host probes, after probe fix | 0 | 5/5 | 86016 | 48320 | 19 | 119287808 | 246505472 | 74 |
| 1792214703501310731 | vitest probes.test.ts | 1 | 3 failed / 6 passed | 5238784 | 5168661 | 64 | 575123456 | 800567296 | 100 |
| 1792214896811536394 | vitest probes.test.ts | 1 | 1 failed / 8 passed (**F1**) | 5455872 | 5381235 | 67 | 567771136 | 789323776 | 100 |
| 1792214951061894895 | vitest author file via probes config | 1 | no test files found | 135168 | 71186 | 31 | 404774912 | 580853760 | 86 |
| 1792214967904405203 | vitest.author.config.ts role-leases | 0 | 23/23 | 14036992 | 13920980 | 122 | 616448000 | 833732608 | 100 |
| 1792214989938699450 | vitest.m4.config.ts identity filter | 0 | 2 passed / 16 skipped | 1245184 | 1139748 | 60 | 544284672 | 769564672 | 100 |
| 1792214999805502916 | unittest test_jobs.py | 0 | 56/56 | 528384 | 311376 | 127 | 141504512 | 293588992 | 80 |

Retained failures (not source defects except F1):

1. Host probe `1792214688534855551`: `RuntimeManager` has `execute_job`, not `execute`. Probe bug. Fixed; rerun 5/5.
2. Vitest probes `1792214703501310731`: `configure(config, 1)` hit `/api/roles/[object Object]/1` after the helper became `(project, name, value, generation)`; admin `POST /api/jobs` returned 403 `jobs:create_scope_required` (existing create-scope, not an M7 bypass). Helper/SQL insert corrected.
3. Vitest probes `1792214896811536394`: **F1** (source).
4. `1792214951061894895`: include filter missed `test/role-leases.test.ts`; rerun with `vitest.author.config.ts`.

Review-run peaks: allocated 14 036 992, apparent 13 920 980, cgroup 616 448 000, RSS 833 732 608, tasks 100. Combined owned scratch `.local/review` + `.local/tmp` + `workspace.local/tmp` stayed under the 18 MiB guard (post-run allocated ~0.21 MiB plus this report). Guard never tripped. No unknown artifacts deleted.

---

## Residuals (not additional High findings)

- `claimJob` writer-date expiry uses `this.rawNow().getTime()` inside a `withClockSnapshotAsync` transaction that otherwise uses snapshot `nowMs`. Not independently clock-raced.
- A post-claim HOLD still occupies the role lease until TTL. Same-payload alias HOLD is correct; it does not fix F1.
- Admin session lacks `jobs:create` on `POST /api/jobs` (403). Legacy writer-run in tests is SQL-inserted, matching the author suite. Ordinary legacy root dispatch behavior was not shown to change.
- `authenticateBusRequest` wall clock vs `JobsService` snapshot was not re-probed.
- SQL does not fence OS/provider starts. Uncertain first-action remains M8a/M8b.
- Skipped tests are not passes. Overlapping M4 filters are not extra unique cases.

---

## Limits

- No source, tests, docs-contract, or config edits. No push. No deploys, cloud, DNS, services, timers, enrollment, binaries, Rust, installs, or paid credits.
- No new Claude/fullGLM/provider jobs. No panes. No APLEXER binding override.
- Other live workers in other worktrees left untouched.
- Fake host consumer is not backend qualification. No client body or inventory flag is treated as provider/profile/account proof.

---

## Verdict

`REVISE` exact `abd48b8461a86f577fc6ae8b5c525d9025376558`.

F1 (High) must be fixed on a new exact SHA and re-reviewed. Do not integrate this SHA to main as M7 complete. Host HOLD-before-process is real and necessary; it does not replace single writer-date execution intent in Hub SQL.

`Review: changes-requested sha=abd48b8461a86f577fc6ae8b5c525d9025376558 reviewer=platform-role-leases-review/grok-4.6`
