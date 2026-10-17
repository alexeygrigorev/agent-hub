# M12e R3 — progress (platform-session-r3)

Owner: platform-session-r3 (`ad6431cf-6d46-4508-9eba-078d91c4e833`, OpenCodeGo/MuseSpark1.3).
Parent: `9f9a5fcd5641c8ff79c0f8c9a0515af46c813f4` (AUDITED REJECTION of R2 `2ca4700a`).
Base main: `6bc91a1278250b374ea1d0b11acd353fae51a8cb`.
Scope: own paths only (see task brief §12). No main push; source candidate only.

## Intake (done)

- FIRST ACTION: genuine `a whoami --json > .local/session-r3/first-action-worker.json`
  (tag `platform-session-r3`, ws `/home/alexey/git/agent-hub-wt-platform-session-r3`,
  engine `opencode`, parent `e3f61b6c`).
- `a context --json` checked (2 peers: hub-zai, platform); `a message inbox --json` empty.
- ACK sent to head platform in `/home/alexey/git/agent-coordination`
  (`01a1499b-b998-7be2-bb88-330145b583a9`).
- Read: hub-platform-brief, hub-intake-brief (all 4 addenda), AGENTS.md, implementer role,
  SESSION-CONTROL-R2 (full), SESSION-CONTROL (R1), SESSION-CONTROL.md, SESSION-HOST.md,
  ROUTING.md; `src/session_control.ts` (full), `src/registry.ts`,
  `src/auth/token.ts`, `src/schema.ts`, `src/hub.ts` device/refresh sections.

## Frozen hostauth source (read-only, not copied as code yet)

- `f20b35a60af2de17c91303dd317da70b44f56174` chain `3504795 → ab725d1 → e795f00 → f20b35a`
  in `/home/alexey/git/agent-hub-wt-platform-metrics-r5` (READONLY).
- Extract plan (exact hunks, no M9): `src/host_enrollment.ts` (new 44-line helper),
  `src/schema.ts` (3 `approval_seq` columns), `src/registry.ts` (CAS + authority match),
  `src/hub.ts` approve/issue/refresh narrow hunks. No `metrics.ts`/UI/routes.
- New atomic gap (Codex bF8 partial review): `verifyBearerScope` awaited OUTSIDE txn.
  Fix: crypto `generateTokenPair` async BEFORE txn; truly synchronous live
  kind/scope/expiry/revocation/family/machine checks INSIDE same txn as mutation.
- Reference probes (read-only):
  `.../metrics-r5-review/.local/review/register-atomic*` (1 test),
  `.../host-authority-review/.local/review/novel-legacy*`.
  R2 reviewer probes: `.../session-r2-review/.local/review/novel-worker-probes.test.ts`,
  `test_novel_host_probes.py` (copy only small synthetic probe/configs into own scratch).

## Preventive guards (set up next, before code)

- `umask 077`; `TMPDIR=<repo>/.local/tmp`; `UV_THREADPOOL_SIZE=1 GOMAXPROCS=1
  NODE_OPTIONS=--v8-pool-size=1`; no `rm` with `$`/globs; no blanket TMPDIR wipe;
  existing `node_modules` symlink only, no install; per-run test tmp/cache/synthetic-DB
  roots; in-run 100ms-target monitor from FIRST product test (UTC/allocated+apparent/
  RSS/tasks/exit, abort ONLY own child group at EARLY 16MiB / HARD 20MiB
  under own `.local/.wrangler/dist`); retain all logs/probes/receipts/seed failures.
- Native: max 2 genuinely-own SHELL (no LLM) throwaways `platform-session-r3-probe-*`
  in `own .local/session-r3/native-*`, 128M/32pids/CPU1, fresh quse + head aggregate
  admission BEFORE each start; stop ONLY own 2 IDs; never declare shell as codex.

## Design checkpoint (sent to head, awaiting steer; code/report reading continues)

See head message: authority atomicity, state transitions, native protocol,
engine+cwd/tier, stop oracle. No code edits until head acks checkpoint (reading only).

## Next

1. Commit hostauth prerequisite SEPARATELY FIRST (exact hunks listed above).
2. R2-F2/F3/F4/F5 + full start contract per checkpoint.
3. New suites `test/session-control-r3.test.ts`, `test/host-authority-r6.test.ts`,
   `host/tests/test_session_r3.py` + configs; preserve/run 169.
4. Poll own inbox at design/test/freeze; freeze with SHA/parent/pathlist/digest/matrix.
