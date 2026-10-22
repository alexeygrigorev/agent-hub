# Private intake and orchestrator handoff

The existing `hub` CLI captures requests in the Hub's SQLite Durable Object and
retrieves enough authorized context for a fresh assistant to continue. This
increment provides the intake ledger, API, CLI and durable handoff context.
Assignment through routing, authoritative session linkage, real inventory/report
backfill, assistant adoption, MCP and cross-host session control remain separate
M12 phases. No command here starts agents or executes request text.

## Authority and privacy

All `/api/intake/*` routes require current authentication and return
`Cache-Control: private, no-store`. Existing human administrator sessions need
CSRF for mutations. The existing configured operator admin token also works as a bearer on intake routes, using the existing `HUB_TOKEN` environment override; current hash rotation ends that access. Existing human administrator bearer credentials also work. The public CLI does not depend on cookies. Mixed cookie/bearer requests are refused, cross-origin mutations are refused, and failed registered credentials cannot fall back to operator authority. Ordinary agent bus credentials,
hosts, jobs and routing clients have no implicit intake access. Agent pairing
scopes are unchanged; agents cannot receive administrator authority here.

An operator explicitly grants an existing agent credential read or write access
to a task, project, or all intake. Write includes read. The grant binds the
server's authenticated credential family, canonical project-qualified actor,
registry registration timestamp/incarnation and current machine binding.
Every read page, mutation and retry receipt checks the current credential and
binding again inside the same synchronous transaction as the operation.
Legitimate token refresh retains family authority and idempotency identity;
a new family needs a new grant. Revocation, expiry, registry deletion/recreation,
machine reassignment/deletion and refresh-family revocation fail closed.
Grants expire within 30 days and no later than their credential.

Only current human administrator authority can create/revoke grants, amend
standing rules, or record approval decisions. Operator requests use explicit
control endpoints and retain source, actor and time. Task notes, forwarded text,
status changes and session links cannot grant permission. Even a recorded
operator approval does not change credential scopes or execute its text.

Responses separate:

- `operator_rules`: operator-authored versioned rules with immutable decision
  history, source and capture time. Global rules apply everywhere; project rules
  apply to tasks in that project. Task rule references pin an applicable version.
- `control`: Hub-verified task revision/status and derived continuation state.
  This verifies the ledger transition, never a launched process or accepted code.
- `payload_data`: untrusted title, verbatim requests/history, state descriptions,
  next step, ownership description and uncertainty. Event `authority` labels
  distinguish task data from explicit operator approval records.

Original text stays verbatim in storage. JSON CLI output ASCII-escapes Unicode
and terminal/control characters; parsing the JSON recovers the original text.
API consumers must render text safely and must not treat embedded instructions
as operator policy. Source references, private report paths and task context
are never public discovery data. Errors omit request text and credential values.

## Public CLI

Use the existing `hub` console entry point or `python -m agent_hub_host` with the
same existing token/device login and credential configuration. The existing
`python -m agent_hub_host.cli` entry point also works. No new credential format,
dependency, global binary or local STATE file is needed.

```sh
hub intake create --project example --title 'Review request' --text-file request.txt --key capture-001 --acceptance-criteria 'Review posted; two findings max'
hub intake list --status open --project example
hub intake show TASK_ID
hub intake history TASK_ID
hub intake links TASK_ID
hub intake context TASK_ID
hub intake update TASK_ID --owner example:assistant --next-step 'Obtain review'
hub intake update TASK_ID --version 1 --status blocked --next-step 'Obtain review' --key context-001
hub intake update TASK_ID --acceptance-criteria 'Done means X; verified by Y' --key ac-001
hub intake append TASK_ID --version 2 --event-file event.json --key event-001
hub intake assign TASK_ID --version 3 --key assign-001 --workspace example-ws --engine codex --tier daily --next-step 'Watch Hub binding' --brief-file goal.txt
hub intake grantees --actor example:assistant
hub handoff
hub handoff --project example --page-size 10
```

`--text-file -` reads bounded UTF-8 stdin. Supply `--source-type chat|voice|forwarded|unknown`,
`--source-reference` and `--source-time` when known. Unknown source reference/time
are null; capture time is a separate server timestamp. Voice capture can use
`--voice-pending --source-type voice --source-reference MEDIA_REFERENCE` instead
of text. It records null text and pending transcription immediately. Append a
provided transcript later as another intake event referencing the media; the
original capture stays unchanged. No transcription service is invoked.

The HTTP API requires a stable `key`; task changes require `expected_version`
(`--version` in the CLI). For `hub intake update` and MCP `intake_update`, the
key is generated when omitted. An omitted version is fetched before updating;
only version conflicts retry, with at most three attempts and the same key.
An explicit version makes one write attempt. Uncertain write outcomes are never
automatically retried; the error includes the recovery key. Inspect task history
before retrying, then retry the exact key, version and payload.
A replay returns the original response even if the task has advanced since.
Use a fresh show/context read to get current state. Different payload with the
same key conflicts. A stale revision conflicts without partial writes.

Append concise notes as `{"kind":"note","text":"Context"}`; an omitted source
is recorded as unknown. `session` is an alias for the existing `link` event:
`{"kind":"session","native_id":"external-session"}` records an unverified
reference, while `hub_session_id` requires a real session visible to the caller.
An omitted link provenance is unknown. Assignment responses preserve placement
hold reasons in `hold` and `assigned_session.hold`; a held session has not started.

`hub intake continue TASK_ID --version N --key continuation-001` records a
ready-to-active transition through CAS. It refuses active, uncertain, blocked,
approval-blocked, closed or waiting work. Pending/unavailable voice transcription
is uncertain until a later provided transcript references the same media.
This transition records intent to continue; it does not launch or verify a
session. Use `hub intake update ... --clear uncertainty` to explicitly clear a context field to unknown through CAS. `open`, `in_progress`, `blocked`, `pending_approval`, `done` and
`cancelled` are ledger statuses. They are not routing, review or deploy states.

## Assignment through the canonical Hub session

`hub intake assign TASK_ID --version N --key assign-001 --workspace WS
[--engine E] [--tier quick|daily|deep] [--machine M] [--cwd DIR]
[--brief-file goal.txt | --interactive] [--next-step ...] [--current-state ...]
[--owner ...]` (plus the usual `--source-type/--source-reference/--source-time`
provenance, default `unknown`) creates the task's Hub session through the
canonical session-control service with the caller's own credentials and records
its real session id on the task automatically. There is no second dispatcher:
placement stays with the existing Fleet reserve, the existing model tiers still
decide engine/profile routing (unknown tiers are refused, never silently
upgraded), and no session identity is invented client-side.

The caller needs intake write access on the task AND session-control writer
authority (operator, or an agent credential holding both an intake write grant
and `fleet:launch`). The assigned session is owned by the same actor, so it
stays controllable through the existing `hub sessions list/show/send/capture/stop`
by the same credential; other actors see its id with a `not_visible` marker.

The assignment appends a `link` event carrying `hub_session_id` (server-verified
against the canonical session row: missing or foreign sessions fail closed) and
optionally records `next_step`/`current_state`/`owner` handoff context in the
same CAS-guarded, idempotent mutation. A task whose assigned session is still
live (`queued`, `held`, `offered`, `starting`, `running`, `uncertain`) refuses a
second assignment (`active_assignment_exists`); once the session leaves the
active set the task accepts a fresh assignment. A lost response is retried with
the same key and payload: the Hub session key is derived deterministically, so
the retry replays the same session and the same link response instead of
launching twice. Creating the session is not executing the task: link evidence
remains `external_unverified_not_launch_proof` until the native binding
lifecycle observes the session.

Task views (`show`, `history`, `links`, `context`, `handoff`) carry an
`assigned_sessions` projection with each linked session's live `state`,
`machine`, `engine`, `mode`, `fleet_launch_id`, `binding` (native id, tag,
workspace, generation) and `updated_at`, read from the canonical rows at the
task's snapshot revision. To link a session that was started separately through
`hub sessions start`, append a manual `link` event with `hub_session_id` (same
ownership check, no operator requirement). Direct Fleet `routing_job_id` links
keep working unchanged.

`hub status` (M14a, `src/status.ts`) does not yet join session-control rows; it
shows the assignment's denormalized engine/machine claim as
`claimed_link_unverified`. Joining canonical session lifecycle into status is a
head follow-up outside this increment's scope.

## Acceptance criteria gate (start/assign)

A task is handed to an agent only with non-empty acceptance criteria. The
first-class `acceptance_criteria` field (nullable, max 8 KiB) is set at create
(`--acceptance-criteria` / `--acceptance-criteria-file`) or later through a
CAS update (`--acceptance-criteria`, `--clear acceptance_criteria`), and is
projected in every task view (`payload_data.acceptance_criteria`).

- `POST /tasks/ID/assign` fails closed with `400 acceptance_criteria_required`
  when the task's criteria are missing or whitespace-only. The check runs
  server-side before any Fleet placement/session row, and is re-checked after
  the start round-trip.
- `POST /api/sessions` with `intake_task_id` (CLI `hub sessions start
  --intake-task-id ID`) runs the same server-side gate before placement: empty
  criteria → `400 acceptance_criteria_required`, unknown task id → `404
  unknown_intake_task`. `hub intake assign` passes the task id through, so the
  canonical start path is covered by both gates.
- TBM Phase 2 (admission): every non-interactive (`mode` headless, the
  default) start MUST carry `intake_task_id`; otherwise `400
  intake_task_id_required`. A `done`/`cancelled` task → `409
  intake_task_closed`. Interactive starts are never gated.
- Autonomous starts also pass the TBM quota gate over `tbm_quota_pools`
  (`src/quota_admission.ts`, `evaluateTbmAdmission`): an active kill-switch on
  the fleet pool (`pool_id = '*'`) or on the target engine's provider → `423
  tbm_admission_denied` (`reason: kill_switch_active`); with an explicit
  `engine`, remaining tokens at or below `R_hard = max(5% total, 50k)` → `409
  hard_floor`, at or below `R_soft(t)` (convex decay to `R_hard`, kappa from the
  pool row, 7d cycle assumed until pacing stores it) → `409 soft_reserve`.
  Without an engine only kill-switches apply (per-route floors arrive with the
  Phase 3 router). Malformed pool rows fail closed. Interactive sessions are
  unaffected.
- This is start acceptance (what "done" means before work begins), not
  `owner_acceptance` (completion acceptance recorded after work).

## HTTP contract

All routes use `/api/intake` as a prefix, separate from existing execution jobs.
Mutations are JSON `POST`; reads are `GET`. Identifiers/keys are bounded safe
strings. Unknown mutation fields are rejected. `source` has
`type`, nullable `reference`, and nullable original `timestamp`.

| Route | Behavior |
| --- | --- |
| `/tasks` GET | List authorized tasks; optional `project`, `status`, `limit`, `after`, `revision` |
| `/tasks` POST | Create using `key`, `project`, `title`, `text`, `source`, optional `transcription` |
| `/tasks/ID` GET | Show full task context, history, applicable rules and snapshot version |
| `/tasks/ID/history`, `/links`, `/context` GET | Same complete context projection; no hidden history truncation |
| `/tasks/ID` POST | `key`, `expected_version`, and status/title/current_state/next_step/owner/uncertainty updates |
| `/tasks/ID/events` POST | Append `event` using `key` and `expected_version` |
| `/tasks/ID/continue` POST | CAS ready-to-active context transition using `key`, `expected_version` |
| `/tasks/ID/assign` POST | Create the canonical Hub session (`key`, `expected_version`, `session`, `provenance`, optional handoff context) and record its real id automatically |
| `/handoff` GET | All authorized open work, unresolved obligations and applicable operator rules, paginated |
| `/rules` GET/POST | Operator-only complete bounded rules / versioned rule append |
| `/grantees` GET | Operator-only active grantable agent credential metadata for an actor (`?actor=ACTOR[&family=FAMILY][&limit=N][&after=ID][&revision=REV]`) |
| `/grants` GET/POST | Operator-only bounded grant listing / explicit grant creation |
| `/grants/ID/revoke` POST | Operator-only durable revocation with `key`, `source` |

Task events are append-only. Each event receives a server ID, authenticated actor
and capture time; immutable `intake_key` records each mutation's retry key. Supported event shapes:

| `kind` | Payload fields |
| --- | --- |
| `intake` | `text`, `source`, optional `transcription` (`provided`, `pending`, `unavailable`, `not_applicable`) |
| `note`, `decision`, `followup`, `blocker`, `pending_approval` | `text`, `source` |
| `link` | Nullable `native_id`, `engine`, `tier`, `host`, `hub`, `routing_job_id`, `hub_session_id` (server-verified canonical session, same-actor/operator only); required `provenance` source |
| `report`, `commit` | `reference`, `source` |
| `rule_reference` | `rule_id`, `version`, `source` |
| `resolution` | Unresolved followup/blocker event `ref`, `text`, `source` |
| `approval_decision` | Pending approval event `ref`, `decision` (`granted`, `denied`, `withdrawn`), `text`, `source`; operator only |

Session links are always `external_unverified_not_launch_proof`; report/commit
references are `external_unverified`. Callers cannot assert verified provenance.
Canonical `routing_job_id` links require operator authority and an existing job;
agent writes refuse before looking up existence. `hub_session_id` links require
the real canonical session row and the session's owning actor (or the operator);
missing or foreign sessions fail closed, and the live `assigned_sessions`
projection (state, machine, engine, binding) is read from those rows, never
asserted by the caller. A canonical
reference still does not prove actual start. Later backfill phases
must add verified launch/inventory evidence through their own current authorities.

Rule mutations use `key`, stable `id`, `expected_version` (zero initially),
nullable `project`, `text`, `source`, and explicit `decision`. Scope is immutable;
all amendments append history. Grant creation uses `key`, `credential_id`,
`scope` (`task`, `project`, `all`), nullable `target` (null for all), `access`
(`read`, `write`), integer epoch-millisecond `expires_at`, and `source`.
The operator can use `hub intake grantees --actor ACTOR [--family FAMILY] [--limit N] [--after ID] [--revision REV]` to discover active grantable agent credential metadata for an actor, `hub intake rules|grants --payload-file FILE` or
`hub intake revoke ID --payload-file FILE`; omit payload-file to list rules/grants.

## Complete handoff and concurrent work

A handoff contains all authorized open tasks, original requests, decisions,
applicable rule versions/history, state, next step, sessions/hosts, report/commit
references, pending approvals and unresolved obligations. Closed tasks remain
included while a followup, blocker, approval, voice transcript or explicit uncertainty is unresolved. There is no automatic
expiry, identity recycling or deletion to hide unfinished work.

Each page contains `snapshot_version`, `items`, `total`, `next`,
`authorization_valid_until` and an explicit authorized-scope label. Continue with
`after=next&revision=snapshot_version`; agents also send the returned
`authorization_valid_until`. The next page rechecks live grants. Any intervening
intake write conflicts, and an expired grant invalidates the authorization
snapshot. This uses refusal on stale context rather than keeping historical
private snapshots accessible after revocation.

`hub handoff` assembles every page before printing, verifies the revision,
authorization lease, counts and duplicate IDs, and retries a stale snapshot at
most three times. It emits no partial briefing as complete. Task-scoped readers
see their authorized subset explicitly; an operator can authorize a fresh
assistant to all open work through an all/read or all/write grant. A subsequent
context read can supply `revision` to detect a stale briefing; task mutations
still require CAS. No previous chat, local handoff file or local STATE is used.

## Capacity and validation

Limits are explicit: 32 KiB streamed UTF-8 HTTP input; 8 KiB request/event text;
256 tasks; 128 events per task; 128 rules with 128 amendments each; 256 grants;
8,192 retry receipts; 128 KiB stored task; 256 KiB individual stored record; and
4 MiB combined persisted JSON plus IDs. References have separate smaller bounds.
Pages contain at most 25 complete tasks. Session links share the per-task event
limit. Rules, receipts and revocations consume the same global byte budget.
SQLite/index overhead is additionally bounded by row counts; these are logical
payload limits, not a disk accounting estimate.

Exhaustion returns an explicit 507 capacity error, oversized streamed input 413,
invalid fields 400 and stale revision/idempotency conflicts 409. The transaction
rolls back task, history, receipt and global revision together. Successful retry
receipts are retained; no TTL silently enables duplicate intake after restart.
Capacity expansion or archival needs a separately reviewed operator workflow.

Focused checks use the real Worker/Hub SQLite HTTP path and stdlib CLI mock HTTP:

```sh
node_modules/.bin/vitest run --config test/vitest.intake.config.ts --configLoader runner
node_modules/.bin/tsc --noEmit
PYTHONPATH=host python3 -m unittest host.tests.test_intake -v
```

All fixtures are synthetic. These checks establish source behavior. They do not
establish deployment, actual routing/session execution, physical assistant
adoption, completed backfill, cross-host orchestration or full M12 acceptance.
