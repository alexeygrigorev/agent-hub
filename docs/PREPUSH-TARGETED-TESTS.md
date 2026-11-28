# Targeted pre-push verification (intake cb5c362e)

Problem (2026-10-31 17:47): every session ran the full suite (`test-full-local.sh`,
10+ min of single-worker Vitest) through two host-wide heavy-test slots. Twelve
sessions queued (median wait so far 509 s, longest 1323 s, still growing) and one
commit reached main in 40 minutes.

## Rule

Follow [AGENTS.md](../AGENTS.md): work in an isolated task workspace, run
`scripts/test-affected-local.sh` before pushing the finished PR branch, obtain
independent review of the exact final head and green required CI, then enable
`gh pr merge --auto --rebase --match-head-commit <reviewed-sha>`. Do not push
main directly. A selector result of BROAD (exit 4) sends the broad/full suite to
CI; never run `scripts/test-full-local.sh` or a broad suite on Hetzner.

## Retained main-push hook (legacy mechanism)

`scripts/push-main.sh` and the managed pre-push hook remain in source for legacy
callers. They are not the current PR shipping procedure and do not authorize a
direct main push. The helper fetches origin/main and installs the hook; the hook
uses Git's advertised main SHA to run the affected lane. Its implemented BROAD
fallback still invokes the full local gate. That retained behavior is not
permission to use it on Hetzner; use the PR/CI flow above instead.

Hub installs the hooks from `host/agent_hub_host/prepush.py` in
`worktree_guard.protect_bound_worktree` before launching/adopting an Agent Hub
session checkout. The primary checkout gets the Scope `commit-msg` hook; linked
worker worktrees get that hook plus the private durable pre-push gate under the
worktree's Git metadata directory. Each installed path uses a per-worktree
`core.hooksPath` (`extensions.worktreeConfig` enables this Git capability), so
the primary checkout's pre-push behavior and other projects' hook settings stay
unchanged. A repository policy marker preserves enforcement if a candidate
removes the affected lane. Canonical Agent Hub origins are also recognized when
the lane is missing.

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
A stale main update must be reconciled before retrying. The helper does not
grant integration or deployment authority.

The runner requires the `capped-test-retry.sh` direct routing for self-admitting
lanes (introduced in d8740aac); older checkouts must update before using it.
The trusted worker preamble requires the Scope commit hook and the pre-push gate,
and forbids disabling them or using `--no-verify`. Local hooks enforce ordinary
pushes; intentional Git hook-disable options remain outside this local control.
Post-push CI remains required.

Regression coverage: `host/tests/test_push_main.py` uses disposable bare origins,
real commits, affected-test selection, test execution and pushes. It covers both
helper and plain pushes, a failing commit leaving origin/main unchanged, a
repaired commit pushing, automatic Scope trailers on normal and merge commits,
gray trailer preservation, the primary checkout's Scope-only hook, and linked
worker pre-push protection, other-project exclusion, inherited-hook preservation,
missing lanes, refspecs, source mutation and BROAD failures. These tests describe
the retained mechanism. Fix-forward and revert work follow the current PR, review and CI procedure above.

## Current affected lane and CI

- Before a PR-branch push: `scripts/test-affected-local.sh` (optionally `--base REF`,
  `--dry-run` to print the selection). It runs scope guard, workflow validation
  if `.github/workflows` or `scripts/ci` changed, then inside ONE slot: typecheck
  (if TS changed), Vitest on the affected files, pytest on the affected files
  with a disposable HOME and scrubbed session env, and the Worker dry-run build
  (if `src/` or wrangler changed).
- Required CI is split across `.github/workflows/deploy.yml`, `hub2.yml`
  (including integrated start-path e2e), and `python-tests.yml` (host/CLI and
  release tooling), with their configured triggers and reusable jobs. Deploy
  follows the required green gates. Fix new CI failures forward on the PR; do
  not exclude them or treat a local targeted pass as a full-suite pass.
- Selection (`scripts/ci/affected_tests.py`): changed test files; tests whose name
  starts with the changed module's stem; tests that mention the changed path or
  import the module (`agent_hub_host.X`, `from agent_hub_host import X`,
  `import X`, `X.py/.ts/.js/.html`). Docs-only changes select nothing.
- BROAD (exit 4): `package.json`, lockfiles, `vitest.config.ts`, `wrangler.jsonc`,
  `tsconfig*`, `pyproject.toml`, `uv.lock`, `host/tests/conftest.py`, the heavy-test
  gate/scripts. Run those broad/full checks in CI, not locally on Hetzner.
- Use the shared `AGENT_HUB_HEAVY_TEST_DIR`; its live `slots` file controls
  admission under the maintained cap. Never change it or unlink live locks to
  admit a run. A targeted run holds a slot ~40 s instead of 10+ min.

## Measurement (Hetzner, 2026-10-31, load average 13-18)

One targeted run (change to `src/quota.ts`; 6 Vitest files, 6 pytest files,
typecheck, build): 38 s wall.

10 concurrent sessions launched at the same instant against 2 slots, wait until
admission (seconds): 2.6, 3.1, 40, 42, 79, 82, 115, 118, 151, 153. **Median 80 s,
max 153 s** (target < 180 s median). Before: the live queue at 17:51 held 13 tickets
with median wait 509 s so far, max 1323 s, each admitted run taking 10+ min.
This historical measurement used a private lock dir with 2 slots (two extra
concurrent runs for ~3 min). It does not specify the current shared slot count.

Option 2 (offload to DESKTOP-FP4OP26) was not needed and is not implemented.
