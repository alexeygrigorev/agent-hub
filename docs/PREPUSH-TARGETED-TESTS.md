# Targeted pre-push verification (intake cb5c362e)

Problem (2026-10-31 17:47): every session ran the full suite (`test-full-local.sh`,
10+ min of single-worker Vitest) through two host-wide heavy-test slots. Twelve
sessions queued (median wait so far 509 s, longest 1323 s, still growing) and one
commit reached main in 40 minutes.

## Rule

Hub workers and the orchestrator-main release-slot owner push Agent Hub main
with `scripts/push-main.sh` after committing the finished change and obtaining
the release slot. Run it from the checkout being released; it accepts no skip,
remote, or ref arguments. Use this helper instead of a raw `git push`.

The helper fetches origin/main, installs the managed Git pre-push hook and
pushes only the committed HEAD. The hook is the single verifier for both the
helper and plain `git push origin HEAD:main` (including `worker:main` refspecs).
It receives Git's advertised remote main SHA and runs
`scripts/capped-test-retry.sh -- scripts/test-affected-local.sh --base <sha>`
with final priority. BROAD (exit 4) runs the full local gate. Tests run once per
push, with no cached pass, environment skip flag or helper-only exception.

Hub installs the hook from `host/agent_hub_host/prepush.py` in
`worktree_guard.protect_bound_worktree`, before launching/adopting an Agent Hub
worker in a created, resumed or existing linked worktree. Each worker gets a
private durable gate under its Git metadata directory and a per-worktree
`core.hooksPath` (`extensions.worktreeConfig` enables this Git capability).
The main checkout and other projects keep their hook settings. A repository
policy marker preserves enforcement if a candidate removes the affected lane.
Canonical Agent Hub origins are also recognized when the lane is missing.

Inherited hooks remain effective: the previous pre-push hook receives the same
ref input and must pass; other executable hooks are forwarded to their original
paths. The originals are preserved. An installation failure without a working
gate refuses worker launch rather than allowing an ungated main push. The
redacted launch diagnostic uses `dispatch_worktree_refused` with the specific
`error_type=PrepushHookInstallError`. Unusual
repositories with shared `core.worktree` or bare settings require configuration
migration before per-worktree hooks can be enabled; the installer does not
rewrite those settings. To install explicitly for an existing worker:
`python3 host/agent_hub_host/prepush.py install`. For the existing linked Agent
Hub-bound workers in a repository (a valid `hub-session` worktree lock), run `python3 host/agent_hub_host/prepush.py
install-workers`; it skips the main checkout, missing worktrees, unbound human
worktrees and manual/integration locks. New/adopted
workers are installed individually by the host.

The hook requires a clean tree, pushed SHA equal to HEAD, an existing main
base and fast-forward ancestry. Failures, exhausted capacity retries, absent
runners, deletion/creation of main or source changes during tests refuse the
push before any remote ref update. Non-main pushes pass through. Repositories
without an affected-test lane refuse main updates until that lane is supplied.
A shared Git-directory lock covers only the final fetch, comparison with the
advertised main SHA, and source recheck. Tests and admission waits run before
acquiring it, including the BROAD fallback; the runners retain their heavy-test
admission. The hook releases this short lock before returning to its parent Git.
Git's remote ref transaction locks and compares the advertised old SHA atomically
when updating main, so a concurrent remote push is refused even after the hook.
Integrate and rerun after main advances. The orchestrator release slot covers
verification through push; this helper does not grant it.

The runner requires the `capped-test-retry.sh` direct routing for self-admitting
lanes (introduced in d8740aac); older checkouts must update before using it.
The trusted worker preamble requires the hook and forbids disabling it or using
`--no-verify`. Local hooks enforce ordinary pushes; intentional Git hook-disable
options remain outside this local control. Post-push CI remains required.

Regression coverage: `host/tests/test_push_main.py` uses disposable bare origins,
real commits, affected-test selection, test execution and pushes. It covers both
helper and plain pushes, a failing commit leaving origin/main unchanged, a
repaired commit pushing, separately protected worker worktrees, main/other-project
exclusion, inherited-hook preservation, missing lanes, refspecs, source mutation
and BROAD failures. Fix-forward and revert commits use the same gate: test the
repaired/reverted tree and push when green.

- Before a push: `scripts/test-affected-local.sh` (optionally `--base REF`,
  `--dry-run` to print the selection). It runs scope guard, workflow validation
  if `.github/workflows` or `scripts/ci` changed, then inside ONE slot: typecheck
  (if TS changed), Vitest on the affected files, pytest on the affected files
  with a disposable HOME and scrubbed session env, and the Worker dry-run build
  (if `src/` or wrangler changed).
- The full suite runs once per push in `.github/workflows/deploy.yml` (unfiltered
  Vitest, critical Python, browser checks, build). Deploy only follows a green
  run. A red full run is fixed forward by the pusher.
- Selection (`scripts/ci/affected_tests.py`): changed test files; tests whose name
  starts with the changed module's stem; tests that mention the changed path or
  import the module (`agent_hub_host.X`, `from agent_hub_host import X`,
  `import X`, `X.py/.ts/.js/.html`). Docs-only changes select nothing.
- BROAD (exit 4): `package.json`, lockfiles, `vitest.config.ts`, `wrangler.jsonc`,
  `tsconfig*`, `pyproject.toml`, `uv.lock`, `host/tests/conftest.py`, the heavy-test
  gate/scripts. Run `scripts/test-full-local.sh --priority final` for those.
- The cap is unchanged: same six slots, same `AGENT_HUB_HEAVY_TEST_DIR`, FIFO.
  Only the work per push shrank. A targeted run holds a slot ~40 s instead of 10+ min.

## Measurement (Hetzner, 2026-10-31, load average 13-18)

One targeted run (change to `src/quota.ts`; 6 Vitest files, 6 pytest files,
typecheck, build): 38 s wall.

10 concurrent sessions launched at the same instant against 2 slots, wait until
admission (seconds): 2.6, 3.1, 40, 42, 79, 82, 115, 118, 151, 153. **Median 80 s,
max 153 s** (target < 180 s median). Before: the live queue at 17:51 held 13 tickets
with median wait 509 s so far, max 1323 s, each admitted run taking 10+ min.
This test used a private lock dir with 2 slots (two extra concurrent runs for
~3 min); the shared dir keeps its 2-slot cap.

Option 2 (offload to DESKTOP-FP4OP26) was not needed and is not implemented.
