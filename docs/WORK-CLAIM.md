# Work claims: who is already on this (MUST-write rules)

Every worker, coordinator, one-shot fixer, and reviewer that touches a
repository **MUST** declare its work before mutating files or spawning
subagents, so operators and peers see who is already working and skip
duplicates. The read side is [docs/WORK-ACTIVITY.md](docs/WORK-ACTIVITY.md)
(`hub activity`, Fleet/Sessions panels, `GET /api/bus/activity`); this
document is the write side. Protocol schemas and kinds follow the
work-claim design document exactly (`hub.work-claim.v1`,
`work.claim|complete|abandon|steal|handoff`, `work.heartbeat` by refresh
only) — no competing protocol.

## 0. Preflight (mandatory before starting or editing)

```bash
MAIN=/home/alexey/git/agent-hub   # canonical primary checkout, never the worktree
a context --workspace "$MAIN" --json
a message log --workspace "$MAIN" --json
hub activity --project agent-hub
```

If an active, non-stale peer already owns the same `work_key`
(`task:<uuid>`, `ci:<owner>/<repo>:...`, …) or an overlapping `edit` scope,
do not start a duplicate: coordinate, narrow `--paths`, or wait.

## 1. Claim on start

```bash
a work join "$MAIN" \
  --task "task:<uuid> [claim=<claim_id>] [ttl=1800s] | <short summary>" \
  --mode edit \
  --paths src --paths host/agent_hub_host --paths docs

a message send --all --kind work.claim \
  --data '{"schema":"hub.work-claim.v1","event":"claim","claim_id":"<claim_id>","seq":1,
    "work_key":"task:<uuid>","project":"agent-hub","workspace":"'"$MAIN"'",
    "mode":"edit","scopes":["src"],"holder":{"machine_id":"<host>","session_id":"<sid>","tag":"<tag>"},
    "lease_ttl_sec":1800,"summary":"<short summary>"}' \
  "CLAIM task:<uuid> [edit: src] by <tag>"
```

- `--mode edit` only when editing; observers use `--mode read`/`review`
  (never exclusive, never blocking).
- Heartbeats only re-run `a work join` (bump `updated_at_ms`); never spam
  `work.heartbeat` messages into the 1000-message mailbox ring.
- `claim_id` matches `^clm_[0-9A-HJKMNP-TV-Z]{10,26}$`.

## 2. Release on exit (always, including failure)

```bash
a message send --all --kind work.complete \
  --data '{"schema":"hub.work-claim.v1","event":"complete","claim_id":"<claim_id>","seq":2,
    "work_key":"task:<uuid>","project":"agent-hub","workspace":"'"$MAIN"'",
    "mode":"edit","scopes":["src"],"holder":{...},"lease_ttl_sec":1800,
    "outcome":{"status":"completed","fix_commit_sha":"<sha>","report_path":"<path>"}}' \
  "COMPLETE task:<uuid> commit=<sha>"
a work leave "$MAIN"
```

Blocked/cancelled work emits `work.abandon` (`reason: blocked | ...`)
instead, then `a work leave`. A dead session's stale claim is taken over
with `work.steal` only after verifying `stale: true` in `a context`.

## 3. Canonical mailbox rule (worktree trap)

`aplexer` hashes the **sender session's record workspace** for mailbox
routing: a message sent from inside a temporary worktree lands in the
**worktree's mailbox even when run with `cwd` set to the primary checkout
and `APLEXER_SESSION_ID`/`APLEXER_TAG`/`TMUX` unset**. `--workspace <MAIN>
--all` is rejected by the CLI, and the worktree mailbox is deleted when the
worktree is removed.

Consequences:

- Always pass the canonical primary checkout (`$MAIN`, resolved via
  `dirname $(git rev-parse --git-common-dir)`) to `a work join`.
- The host daemon scans **all** mailboxes whose workspace equals a
  configured repo or shares its `git_common_dir`, so `work.*` events are
  still bridged to Hub even from worktree mailboxes — but peers reading
  `a message log --workspace "$MAIN"` locally will NOT see worktree-mailbox
  messages. Prefer starting sessions with `--workspace "$MAIN"` when the
  CLI supports it.
- `a context --workspace "$MAIN"` correlates sibling worktrees via
  `git_common_dir` regardless of mailbox placement: always check it.

## 4. No secrets on the bus (allowlist + scrubbing)

`work.*` payloads carry identifiers only (`claim_id`, `work_key`,
`task_id`, `session_id`, `mode`, `scopes`, `commit_sha`, `branch`,
`report_path`, `summary` ≤ 280 chars). Forbidden: raw CLI args, env dumps,
diffs, stack traces, email bodies, tokens, URLs with credentials or secret
query params. Client, host bridge, and Hub server all scrub token patterns,
bearer/env assignments, AWS keys, PEM blocks, URL credentials/secret query
params, and high-entropy (≥ 40 char) runs before storage. When in doubt,
leave the field out.

## 5. How Hub surfaces native work

The host poll cycle (`Dispatcher.once`) runs `workclaim.sync_once`, which
reads live `coordination.json` declarations and new `work.*` envelopes from
canonical mailboxes, validates + redacts them, and POSTs to
`POST /api/bus/native-sync` with the host credential:

- `edit` declarations become native claim rows (`holder:
  native:<machine>:<tag>[<short>]`, `source: native`); `read`/`review`
  rows are non-exclusive observations that never block Hub admission.
- `work.*` envelopes become redacted `project:<project>` messages
  (idempotent via `aplexer-msg:<uuidv7>`).
- Terminal events (`complete`/`abandon`) release the synced rows; TTL
  expiry reaps ghosts, like Hub-native claims.
- `GET /api/bus/activity` merges native rows into `claims` /
  `recent_messages` with `source`, `mode`, `task_id`, and `stale` fields.

Workspace → project resolution seeds `/home/alexey/git/agent-hub →
agent-hub` and `/home/alexey/git/aplexer → aplexer` (visibility-only,
`require_claim = 0`); operators extend it via `PUT
/api/fleet/claim-policy`. Manual one-shot sync for debugging:
`hub workclaim-sync --dry-run` (scan only) or without `--dry-run` (POST).
