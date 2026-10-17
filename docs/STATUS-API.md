# M14a Status API (`GET /api/status`) and `hub status` CLI

M14a only: one authorized read projection over existing canonical records.
M14b (native/report collectors), M14c (verified Git/active deployment/CI adapters)
and M14d (durable caller-scoped last-call diffs) remain explicit successor work.

## Endpoint

`GET /api/status?limit&after&project&task&host&since` — readonly, `Cache-Control:
private, no-store`. Non-GET returns 405. Unknown query keys return 400.
Exactly one SQLite read transaction per call; no writes, no native input,
no DB cursor advance (`intake_meta.revision` unchanged).

## Auth (reuses accepted IntakeService)

Same authenticate callback as `/api/intake`: rejects mixed
Authorization+Cookie authority, supports operator token fallback and cookie
sessions. Inside the synchronous transaction the caller is resolved through
`IntakeService.statusPrincipal` (canonical actor, current credential, grants,
family, registry incarnation, machine binding). `fleet:launch` or
`routing:submit` alone grants nothing. Expired, revoked or superseded families
reject without data. Task, workspace and host scope constrain every section
and page.

## Join rule (forged links cannot escalate)

History `link` events are `author_claim` (`claimed_link_unverified`) unless a
`routing_jobs` row exists AND its `fleet_launches` row matches AND a
`fleet_sessions` row matches on `(machine_id, native_id)`. Only then
`verified_launch`. A missing or stale host yields `partial` coverage with
`missing_hosts`, never empty or healthy.

## Freshness

`fleet_sessions.observed_at` means LAST CHANGED, not last seen; it is never
restamped with heartbeats. `phase=running` proves nothing about useful work.
No session-lifecycle classification is applied in M14a. No raw terminal,
prompt, draft or report-body reads.

## Per-task DTO

- identity: `id/project/status/continuation_state/version/updated_at/created_at/title`
  (title truncated; full history bodies never exported).
- `sessions[]`: machine/native/tag/engine/profile/phase/agent_state/workspace/
  account/observed_at/last_activity_at/linkage/launch_id/routing_job_id plus
  per-row provenance `{source, observed_at, freshness, coverage, reason}`.
- `reports[]`: `{reference, source}` plus `headline: unknown`,
  `verdict: unknown` with `successor: M14b`.
- `commits[]`: `{reference, source}` plus `deploy_state: unknown`,
  `ci_state: unknown` with `successor: M14c`. A linked commit or CI green is
  never deploy proof.
- `pending_actions[]`: intake unresolved `followup/blocker/pending_approval`
  only as `{id, type, status, actor, captured_at, text_snippet<=240, link}`.
  Login `user_code` values are always withheld from agent/orchestrator bearers
  in this cut; `device_code_hash`, tokens and credential hashes never appear.
- `session_coverage` per task plus top-level `coverage` and `categories`
  (tasks_sessions / reports / commits_deploys_ci / pending_actions / diff)
  with honest `unknown` for uncollected successors.

## Pagination and `--since`

`limit` 1..25 (default 25), `after` task id, `project/task/host` filters,
`since` as `<N>m|h|d` or ISO-8601. `since` filters available timestamped rows
only. There is no durable caller baseline yet: `categories.diff.state` is
always `not_available` with `successor: M14d`, never a fabricated zero-change
diff. Cursors are caller-scoped task ids; filter changes cannot leak another
caller's rows.

## CLI

`hub status [--since 2h] [--json] [--task ID] [--project P] [--host M]
[--after ID] [--limit N] [--local-only]`

One CLI invocation makes exactly one `GET /api/status` request using the
existing token/device auth. `--local-only` preserves the old local-config
diagnostic without any API call. Human output escapes terminal controls;
`--json` prints the raw DTO.
