# Intake worktrees: registry, resume, close gate, remove

Intake 4b161918. Every intake owns its own dedicated git worktree(s). Hub
records them, resumes follow-up sessions in them, refuses to close an intake
while any of them is unmerged or still on disk, and removes them on the owning
host through the host agent. Complements the disk-GC intake 1f1f1877, which
owns any automatic deletion; this workflow never deletes automatically.

## Registry (Hub)

Table `intake_worktrees`, one row per (intake, machine, path):

| field | meaning |
| --- | --- |
| `id` | uuid |
| `task_id` | owning intake |
| `machine` | Hub machine id of the owning host |
| `path` | absolute worktree path on that host |
| `branch` | local branch name (null when unknown) |
| `created_at` | first record time (ms) |
| `session_ids` | Hub session ids that used it |
| `origin` | `hub_created`, `hub_resumed`, `hub_recreated`, `session_cwd`, `registered` |
| `state` | `active`, `removed` (Hub remove succeeded) or `gone` (host reported it absent and merged) |
| `status` | last host-reported status, or null (unknown) |
| `last_remove` | last remove result `{result: ok|refused|error, reason, at}` |

No sharing: an `active` (machine, path) pair belongs to exactly one intake.
Registering or starting a session whose absolute `cwd`/`workspace` is another
intake's active worktree is refused with `worktree_owned_by_other_intake`.

`hub intake show` and the intake page carry `worktrees` (rows above plus
pending commands) and `close_gate` (`{ok, blocking}`).

## Resume (AC2)

On every session start linked to an intake (`hub sessions start`, `hub intake
assign`, forgotten restart: they all go through `POST /api/sessions`), when the
intake has an active worktree and the start names no machine, Hub pins the
start to that worktree's machine. When the launch is offered, the host agent
asks Hub for the plan:

`POST /api/worktree-host/plan {launch_id}` returns one of

- `{mode: "none"}`: no intake, or the start named its own cwd. Launch as before.
- `{mode: "none", task_id, detached: true}`: the start set
  `intake_worktree: "detached"` (intake 79458f2d). It keeps the intake link but
  runs in its workspace root: Hub never pins it to the worktree's machine,
  resumes, creates or locks the intake worktree for it, and never counts it as
  a live session bound to that worktree. A detached start may not name a
  workspace or cwd with any `-wt-` path segment (the tree, a subdirectory or a relative name). The hourly backlog triage and the post-DONE
  verifier use it.
- `{mode: "resume", task_id, worktree_id, path, branch}`: use that worktree. If
  the directory is missing on the host, create it again from fresh
  `origin/main` at the same path (`git worktree add`), then report `recreated`.
- `{mode: "create", task_id, suffix}`: create `<dirname(ws)>/<basename(ws)>-<suffix>`
  on branch `<basename(ws)>-<suffix>` from fresh `origin/main`, where `ws` is
  the resolved workspace repo and `suffix` is `wt-<first 8 of intake id>` for
  ordinary starts, or `wt-r-<first 8 of the session id>` for successor starts.

### Successor starts (intake afab8367)

A successor start (`intake_worktree: "successor"`) is the idle-work
replacement of a LIVE predecessor. Idle-work keeps the predecessor running
until the replacement itself is running, but the host worktree lock only
moves to a proven-terminal owner (intake 1c8d915e), so a replacement into the
same worktree is always refused as `dispatch_worktree_refused`. Instead Hub
plans a fresh sibling worktree (create mode above): the replacement runs in
its own workspace, the predecessor's tree stays locked and untouched, and no
lock is ever stolen or force-unlocked. A successor start may not name a `cwd`
(it would resolve against the live-held tree). The predecessor's uncommitted
state is reported, never copied or force-taken: when idle-work submits the
replacement it queues a `status` command for the held worktree and notes the
situation on the intake; the dirty/unpushed/unmerged report lands in the
worktree registry row (visible via `hub worktree list`). Successors of the
same intake therefore leave several active worktree rows; the close gate and
the remove guards cover all of them.

The host then launches with cwd = the worktree and reports
`POST /api/worktree-host/observe {launch_id, action, path, branch, error?}`
with `action` in `created|reused|recreated|failed`. A failed creation (no git
repo, no origin/main, guard refusal) is recorded on the intake and the launch
proceeds in the workspace as before: no launch is lost to this feature.

## Close gate (AC3)

Transition to `done` (`hub intake close`, `hub intake update --status
done|closed`, the page's Close button) checks every non-removed worktree of the
intake against its last host status. It passes only when the worktree is
`removed`/`gone`, or the status says `present=false` and `merged=true`. Any
other state refuses with HTTP 409:

```json
{"error": "worktree_close_gate", "worktrees": [
  {"worktree_id": "...", "machine": "...", "path": "...", "branch": "...",
   "problems": ["still present", "unmerged commits 2", "dirty", "status unknown"]}],
 "hint": "hub worktree refresh <intake> | hub worktree remove <intake> | --force --reason"}
```

`force: true` plus a non-empty `reason` overrides; Hub appends a
`close_gate_override` history event (actor, reason, blocking list) and the
audit `close` row carries `force: <reason>`.

## Host commands (AC4, AC5)

A machine-scoped queue `intake_worktree_commands` (kinds `status`, `remove`).
On host check-in, Hub queues a status command for up to 10 active worktrees
whose status is still unknown, respecting the machine queue cap. A failed
unknown-status command is retried after five minutes. This also covers
worktrees registered before this behavior was deployed; no manual refresh is
needed just to populate `status_at`. The host agent polls each cycle:

- `GET /api/worktree-host/commands` returns `{commands: [{id, kind, worktree_id, task_id, path, branch}]}`
  (queued commands are claimed; claims older than 15 minutes are re-offered).
- `POST /api/worktree-host/commands/<id>/result {outcome, reason?, status?, removed?, branch_deleted?}`
  with `outcome` in `ok|refused|error`.

`status` shape (unknown is `null`, never invented):
`{present, dirty, dirty_count, unpushed, unmerged, merged, head, branch, checked_at, error}`.
`unmerged` is the count of `git cherry origin/main HEAD` `+` lines after a
fresh `git fetch origin main`; `merged` is true when HEAD is an ancestor of
`origin/main` or every commit is cherry-equivalent. `unpushed` counts commits
not on any remote ref (`git rev-list HEAD --not --remotes`).

Remove refuses (and records why) when: the path fails the guard, the worktree
is dirty, has unmerged or unpushed-and-unmerged commits, a process has its cwd
inside it, or a live/held Hub session is bound to it (checked on Hub before
queuing). It runs `git worktree remove <path>` (never `--force`, never rm),
`git worktree prune`, and `git branch -d <branch>` only when merged (never `-D`).

Path guard: the realpath must equal the path, sit directly under `$HOME/git/`,
have a basename containing `-wt-`, and never contain `/prod`.

User endpoints (intake auth and grants):

- `GET  /api/intake/tasks/<id>/worktrees`
- `POST /api/intake/tasks/<id>/worktrees {key, worktree: {machine, path, branch?, session_id?}}` register
- `POST /api/intake/tasks/<id>/worktrees/refresh {key, worktree_id?}`
- `POST /api/intake/tasks/<id>/worktrees/remove {key, worktree_id?}`
- `POST /api/intake/worktrees/remove {key, session_id}`
- `GET  /api/intake/worktree-commands/<command id>`

CLI: `hub worktree list|refresh|remove|register <intake|session>`,
`hub intake close <id> [--force --reason TEXT]`,
`hub intake update --status closed` (alias of `done`) `[--force]`.

## Terminal lock release and repair

When a Hub session and its native incarnation are positively terminal, the
host releases its worktree lock only if the current Git lock reason names that
exact session (and intake when present). A live successor that has taken
ownership keeps its own lock. Open intakes remain protected from automatic
cleanup through the separate cleanup guard; they do not retain a dead
session's lock.

On the owning host, `hub worktree repair-locks --dry-run` lists existing
Hub-session locks whose holder is positively terminal, explicitly unprotected
and not human-owned according to Hub, matches the original repository identity,
and exactly owns the lock reason. Review the full listing first, then run
`hub worktree repair-locks --apply` to release eligible locks. The scan is
bounded to 1,000 terminal Hub facts and 30 seconds; an incomplete scan applies
nothing. Repeating the command is safe. It uses `git worktree unlock` only;
it never uses `--force` or removes worktrees. Human-owned or human-protected sessions,
non-terminal sessions, manual locks, foreign locks, unknown repository
identities, and mismatched lock reasons remain untouched.

Legacy sessions (those started before the pre-start launch identity evidence
file existed) have no repository witness on disk, so the identity-based check
above fails closed for them. The repair has a reviewed legacy path for
exactly that case: when no evidence file exists for any canonical channel of
the fact (a present-but-invalid file still refuses), the repository identity
is derived from the locked worktree itself (`git rev-parse --git-common-dir`
plus `git remote get-url origin`) and accepted only when the lock reason
names that exact session and intake, the Hub facts remain terminal,
native-terminal, not human-owned, unprotected and release-permitted, and the
locked path lies under the session's recorded workspace or follows the Hub
worktree naming contract (`<home>/git/<name>-wt-<suffix>`, symlink-free)
with a repo-hint main checkout that shares the worktree's origin URL. The
same bounds, dry-run default, idempotent apply and never-`--force`/
never-remove rules apply; candidates carry a `legacy` marker and the derived
origin so the dry-run listing stays reviewable.
