# Targeted pre-push verification (intake cb5c362e)

Problem (2026-10-31 17:47): every session ran the full suite (`test-full-local.sh`,
10+ min of single-worker Vitest) through two host-wide heavy-test slots. Twelve
sessions queued (median wait so far 509 s, longest 1323 s, still growing) and one
commit reached main in 40 minutes.

## Rule

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
- The cap is unchanged: same two slots, same `AGENT_HUB_HEAVY_TEST_DIR`, FIFO.
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
