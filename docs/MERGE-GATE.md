# Merge gate: an intake is done only when it is merged and deployed

Intake 2e00e01f (Alexey, 2026-10-31: "a task is closed only if it is merged").
Cause: intake b974e406 was built on 28 Oct (commit 317e1d34) but never merged
or deployed. Review never ran (`engine_auth_expired`), the full suite stopped
halfway and nothing tracked the unmerged branch.

## Evidence (Hub never runs git)

Hub stores one row per intake in `intake_merge_state`: tracked worktree
(machine, path, branch, head), the commits that must be on main
(`{sha, in_main}`), the Deploy result and timestamps. It is fed by

- every worktree recorded for the intake (starts tracking, `state=tracking`),
- every host worktree status result (head, unmerged count),
- `hub intake merge-check <intake> [--repo DIR] [--sha SHA] [--dry-run]`, which
  runs `git fetch origin main`, ancestry (`merge-base --is-ancestor`) for the
  DONE-note commits plus HEAD, and `gh run list --workflow deploy.yml` for the
  exact commits (a later green Deploy that contains the commit counts; `[skip ci]`
  commits are `skipped`; `gh` failure is `unknown`, never green). It posts
  `POST /api/intake/tasks/ID/merge-evidence` (MCP `intake_merge_evidence`).

Deploy states: `success`, `skipped`, `not_required` are green; `failure`,
`pending`, `missing`, `unknown` block.

## AC1 close gate

Closing (`hub intake close`, `update --status done`, MCP `intake_update`, the
web Close button: all hit the same service transition) returns 409
`merge_gate` for an intake with tracked code unless every commit is in
origin/main and the deploy is green. The body names the problems and the
unmerged commits:

```json
{"error":"merge_gate","state":"tracking","problems":["commits not in origin/main: 317e1d34c1aa","deploy not green: unverified (run hub intake merge-check)"],"unmerged_commits":["317e1d34..."]}
```

Operator override is `--force --reason TEXT` (recorded as `close_gate_override`
with `gate: merge`); `--outcome closed_unverified` is unchanged. Intakes that
never recorded a worktree or evidence (research, ops, docs-only) close as before.

## AC2/AC3 watcher and requeue

`MergeGate.due()` reads `(state, due_at)` through an index, 8 rows per scheduled
tick, no table scans. A tracked intake is due 60 minutes after its last activity
(worktree record, status, evidence, intake update, live session). When due:

1. a live session on the intake's worktree defers it (activity);
2. stale merge state asks the host for one `status` refresh, then fails closed
   15 minutes later;
3. not merged and deployed => state `unmerged`, a `UNMERGED:` note on the intake
   and a finish-merge session (same canonical admission as `assign`, `cwd` =
   the worktree, pinned to its host) whose brief says: rebase, run the full
   suite, independent review of the exact version (switch engine/account when
   the reviewer fails, e.g. `engine_auth_expired`, and file a prerequisite),
   push to main yourself, watch CI/Deploy, `hub intake merge-check`, DONE;
4. after 3 requeues the intake becomes `blocked`: Hub files a prerequisite
   through the prerequisite API (`POST /api/intake/tasks/ID/blocker`, intake
   8edc8f7c; before that lands it appends a `blocker` event), raises an
   `intake_unmerged` alert, and returns to `tracking` once the prerequisite is
   no longer open.

States: `tracking`, `unmerged`, `requeued`, `blocked`, `merged`, `closed`.

## AC5 pushing

Sessions push to main themselves when the full tests pass and watch the Deploy
to green. The watchdog restart brief, the finish-merge brief and the orchestrator
brief say so. Nobody waits for orchestrator push permission.

## AC6 visibility

`hub intake merge-state [id]` / `GET /api/intake/merge-state` /
`GET /api/intake/tasks/ID/merge-state` / MCP `intake_merge_state`; the Intake
overview card shows `Unmerged: N` with the list; the intake page shows the merge
chip and the close refusal; `GET /api/intake/forgotten` carries `unmerged`
counts; the daily intake digest lists unmerged intakes.

## AC7 backfill

`python3 scripts/backfill-unmerged-2e00e01f.py [--apply]` scans `~/git/*-wt-*`
worktrees, resolves each to an open intake, records merge evidence and reports
every unmerged one (dry run by default); the Hub watcher requeues them.
