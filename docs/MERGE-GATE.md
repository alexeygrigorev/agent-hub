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
  runs `git fetch origin main` in a private clone of the project repo (`--repo DIR` overrides and also checks that checkout's HEAD; the caller's cwd is never used), takes the latest report SHA as the current head checkpoint, retains every explicit `commit` event SHA, and adds DONE-note hex tokens verified with `git cat-file -e` (uuids and intake/session ids are ignored), ancestry (`merge-base --is-ancestor`) for the
  DONE-note commits plus HEAD, and `gh run list --workflow deploy.yml` for the
  exact commits (a later green Deploy that contains the commit counts; `[skip ci]`
  commits are `skipped`; `gh` failure is `unknown`, never green). It posts
  `POST /api/intake/tasks/ID/merge-evidence` (CLI/REST only: it runs git and gh locally, and the MCP
  tool surface is at its compact-schema budget).

When a report commit is rebased, append a new `report` event for the replacement
SHA with `supersedes: ["<prior-report-event-id>"]`. The target must identify an
earlier report event on the same intake with a commit SHA reference and may be
superseded only once. Merge-check excludes only those explicitly superseded
report events; commit events and malformed or dangling supersession data remain
required evidence, so missing commits still fail closed. Histories without a
`supersedes` field retain the existing latest-report checkpoint behavior; adding
the field opts that intake into explicit event-ID supersession. Supersession
does not alter historical event content.

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
   suite, independent review of the exact version. Review starts use canonical
   Hub routing; after an auth/credential failure the session retries once with
   a distinct provider engine and records a blocker event if that also fails.
   Repeated failed finish-merge attempts create a prerequisite intake and alert
   through the watcher. The session commits and pushes its feature branch, opens
   a PR, lets CI run the full suite, fixes failures from CI logs, then runs
   `gh pr merge --auto --rebase`. It watches CI/Deploy and runs
   `hub intake merge-check` before DONE. It never pushes to main or approves
   deploy gates;
4. after 3 requeues the intake becomes `blocked`: Hub files a prerequisite
   through the prerequisite API (`POST /api/intake/tasks/ID/blocker`, intake
   8edc8f7c; before that lands it appends a `blocker` event), raises an
   `intake_unmerged` alert, and returns to `tracking` once the prerequisite is
   no longer open.

States: `tracking`, `unmerged`, `requeued`, `blocked`, `merged`, `closed`.

## AC5 branch PR and CI flow

Sessions run only targeted unit tests for changed files locally, commit and push
their feature branch, and open a PR. CI runs the full suite; sessions fix
failures from CI logs, then use `gh pr merge --auto --rebase` after required
checks pass. They watch CI and Deploy to green, never push to main, and never
approve deploy gates. The watchdog restart brief, finish-merge brief, and
orchestrator brief must say so.

## AC6 visibility

`hub intake merge-state [id]` / `GET /api/intake/merge-state` /
`GET /api/intake/tasks/ID/merge-state` (and `merge_state` on every intake view, so MCP `intake_show` carries it); the Intake
overview card shows `Unmerged: N` with the list; the intake page shows the merge
chip and the close refusal; `GET /api/intake/forgotten` carries `unmerged`
counts; the daily intake digest lists unmerged intakes.

## AC7 backfill

`python3 scripts/backfill-unmerged-2e00e01f.py [--root DIR] [--apply]` scans
`*-wt-*` worktrees under the shared git directory derived from this checkout
(or the explicit `--root`), resolves each to an open intake, records merge
evidence and reports every unmerged one (dry run by default). It refreshes
`origin/main` once per repository and bounds Git commands; timeouts, fetch
failures, denied intake lookups, and refused apply operations appear as
`inspection_failed`, `fetch_failed`, `lookup_failed`, or `apply_failed` rows
rather than being silently counted as merged or as no-intake work. The Hub
watcher requeues recorded unmerged work.
