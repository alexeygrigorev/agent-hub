# M14 Status API (`GET /api/status`, `POST /api/status/observations`) and `hub status` CLI

M14a shipped: one authorized read projection over existing canonical records.
M14b slice1 shipped: canonical intake-assigned Hub sessions (`hub_session_id`
links into `session_control_sessions`) with live SessionControl state plus
the M15 lifecycle group/decision per bound session. M14b slice2 shipped:
machine-authenticated host observations (session states, sanitized report
headlines) bound to the current canonical session/machine. M14c shipped:
explicit-argv Git ancestry / CI / active-deployment probes with exact-sha
commit matching (no push/CI inference for deploy). M14d shipped:
`POST /api/status` projects the complete bounded summary for all authorized
matching work (up to the 256-task cap, never a silent first page) and
advances the caller's own durable baseline in the same transaction.

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
`verified_launch`. Hub session links (`hub_session_id`) join the canonical
`session_control_sessions` row live at the snapshot revision: operator sees
all linked rows, other callers see only rows whose `actor` equals their own
(`not_visible` otherwise, id only — the link event already discloses the
id); dangling ids render `unknown_session`. A missing or stale host yields
`partial` coverage with `missing_hosts`, never empty or healthy. `win35`
(`DESKTOP-FP4OP26`, intentionally off ~2 weeks from 2026-10-17) is reported
under `coverage.expected_offline` only while the window is active
(`coverage.expected_offline_until: 2026-10-31T15:23:00Z` with
`coverage.expected_offline_reason`); after expiry a stale win35 returns to
`missing_hosts` like any other host, never as an outage and never expected
forever.

## Freshness

`fleet_sessions.observed_at` means LAST CHANGED, not last seen; it is never
restamped with heartbeats. `phase=running` proves nothing about useful work.
Hub-session lifecycle reuses the M15 registry joins + pure classifier (no
second tracker). No raw terminal, prompt, draft or report-body reads.

## Per-task DTO

- identity: `id/project/status/continuation_state/version/updated_at/created_at/title`
  (title truncated; full history bodies never exported).
- available handoff context: `current_state/next_step/owner` (truncated
  carrying values, null when unset) so a fresh agent can continue the task
  from one call.
- `sessions[]`: machine/native/tag/engine/profile/phase/agent_state/workspace/
  account/observed_at/last_activity_at/linkage/launch_id/routing_job_id plus
  per-row provenance `{source, observed_at, freshness, coverage, reason}`.
- `hub_sessions[]` (M14b slice1): per canonical `hub_session_id` link —
  `{hub_session_id, linkage: hub_session|not_visible|unknown_session,
  state, machine, engine, mode, fleet_launch_id, native_id, binding,
  updated_at}` plus `lifecycle: {session_key, group, state,
  reasons[<=5], flags, obligations, decision}` reused from the M15 registry
  joins + pure classifier (no second tracker; unbound intents report
  `unknown` with reason `no_bound_native_identity_or_no_canonical_join`)
  and per-row provenance `{source: session_control_canonical_row, ...}`.
- `lifecycle_summary` per task: `{by_group: {group: count}, decisions[<=10]}`
  over visible `hub_session` links only.
- `reports[]`: `{reference, source}` plus `headline`/`verdict`: `unknown`
  until an allowlisted host reader posts a sanitized headline observation for
  a visible linked session (then `collected_host_reported`, coverage
  `partial`; observations never synthesize report entries). Headlines are
  bounded ≤240 chars, control-stripped and token-redacted server-side;
  transcripts, tokens and screens are never stored.
- `commits[]`: `{reference, source}` plus `deploy_state: unknown`,
  `ci_state: unknown` until an exact-sha probe matches: `git_ancestry`
  (`{sha, ancestry: merged_in_main/not_merged/unknown}`),
  `ci` (`{state: passing/failing/unknown, run}`),
  `deploy` (`{state: deployed/not_deployed/unknown, deployment, sha}`),
  each with `source: host_probe_*` provenance. A linked commit or CI green
  is never deploy proof: `deployed` requires an explicit active-deployment
  record naming the exact sha. Unmatched commits stay `unknown` (no
  borrowing across shas). `categories.commits_deploys_ci` reflects
  collection (`collected_probe_evidence`, coverage `partial`).

Maintained probes: `host/agent_hub_host/status_probe.py` (stdlib only,
standalone `--once --probes SPEC [--submit]`). Probe specs are explicit
argv arrays (no shell): only allowlisted tools (`git/gh/wrangler`) and
read-only subcommands run (`git push/fetch/pull/clone` refused;
`git ls-remote` allowed ONLY as `git ls-remote origin refs/heads/<branch>`;
`gh api` refuses POST/PATCH/PUT/DELETE/`--method!=GET`/`-X`/`--input`;
`gh run` allows `list/view` only, never `cancel/rerun`; `wrangler
deployments` allows `list` only), cwd must sit under allowlisted
`repo_roots`, output bounded IN FLIGHT (chunked drain, 8 KiB kept per
stream, overflow kills the child, `probe_output_truncated`), token-redacted,
child env scrubbed of secrets, argv secrets masked in provenance.
`git_ancestry` reports `merged_in_main` only on exit-0
`merge-base --is-ancestor` AND an exact SHA match between the local ref and
a fresh read-only `git ls-remote origin refs/heads/main` (commit time is
NOT freshness: old commit on a current remote stays fresh, stale local ref
with recent commit never counts; network/parse failures fail closed to
`unknown` with `network_unknown_fails_closed`). `ci` matches runs by exact
`head_sha`; `deployment` matches active records naming the exact sha in the
target environment (push/CI success never implies deployed). Every probe
payload carries its source sha (required).
- `pending_actions[]`: intake unresolved `followup/blocker/pending_approval`
  only as `{id, type, status, actor, captured_at, text_snippet<=240, link}`.
  Login `user_code` values are always withheld from agent/orchestrator bearers
  in this cut; `device_code_hash`, tokens and credential hashes never appear.
- `session_coverage` per task plus top-level `coverage` and `categories`
  (tasks_sessions / reports / commits_deploys_ci / pending_actions / diff)
  with honest `unknown` for uncollected successors. `tasks_sessions.source`
  now reads
  `intake_tasks+routing_jobs+fleet_launches+fleet_sessions+session_control_sessions+lifecycle`.

## Observation transport (M14b slice2)

`POST /api/status/observations` — machine-authenticated host transport for
session states (`working/waiting/context_low/limit_hit/draft_pending/idle/
unknown`), sanitized report headlines, and probe evidence (`probe_git/
probe_ci/probe_deploy`). Additive `status_observations` table (created in
`StatusService.init`, `IF NOT EXISTS`); no task/session registry
duplication, no canonical task mutation, `intake_meta.revision` untouched.

Auth (resolved by Hub BEFORE the mutation transaction): a verified host
credential (`host` scope, kind `host`) names ONLY its own machine, or an
admin operator record (admin session + CSRF, or the operator token
fallback). Agent/job/root bearers carry no observation authority. Inside
the transaction the CURRENT canonical `session_control_sessions` row must
exist and belong to the reporting machine (`hub_session_id` directly, or
exactly one `binding_json.native_id` match for `native_id`; zero or
ambiguous matches fail closed). The injected
`SessionControlService.hostObservationAllowed(machineId, caller)` gate
(existing fresh host family/approval authority) must then allow the write;
while the helper is absent every write fails closed (`503
observation_auth_unavailable`). JSON machine/owner flags never authorize.
Unknown payload fields fail closed; timestamps require explicit `Z`/offset,
reject future skew and >30d age; per-(session, kind) history is bounded to
the latest 20 rows.

Reads join the latest observation per (session, kind) onto already-visible
`hub_sessions[]` entries only (`host_observation`, `report_headline`, each
`host_reported: true` with `source: host_observation` provenance). Host
fields stay data, never operator instructions.

Maintained collector: `host/agent_hub_host/status_collect.py` (stdlib
only, standalone `python3 -m agent_hub_host.status_collect --once
--allowlist PATH [--sessions-json PATH] [--submit]`, omit
`--sessions-json` for real native collection). Reads only
registered/linked sessions (workspace realpath under allowlisted
`session_roots`, optional `sessions` registry; unregistered rows are
skipped with zero observations, never forged) and allowlisted `reports`
entries (path under `report_roots`, bound to a collected session).
Native default reads the ACTUAL aplexer list through
`native_sessions.list_sessions` (existing adapter); `working/waiting/idle`
map only on exact `agent_state`, `limit_hit/context_low` only on explicit
True markers, otherwise `unknown` (no synthetic draft/limit/finished
flags, never inferred from text). Runtime wiring lives in NEW
`host/agent_hub_host/status_runtime.py`: `StatusCollector(...)` with
`step(force=False, submit=False, prepared_sessions=None)` (explicit
allowlist path preferred, sibling `journal/status-allowlist.json` default;
rate-limited, isolated errors, no timer/store/classifier; configured
`probes`+`repo_roots` run inside the same step). Transport is
`api.safe_http_json` (no-redirect, `agent-hub-host/1` UA, bounded JSON;
Cloudflare 1010-safe); errors never carry tokens, URLs, or raw bodies.
Session states come only from explicit structured markers
(`limit_exhausted/context_low/draft_pending/working/waiting/idle`,
limit-first precedence; anything else is `unknown`, never inferred from
text). Headlines are first-heading-only (≤240 chars, sanitized); verdicts
are explicit config (`done/attention`, default `unknown`). Absent/invalid
allowlist fails closed (exit 2, collects nothing). Tokens come from native
secure config/env in memory only (`HUB_URL`/`HUB_TOKEN`/`HUB_JOB_TOKEN` or
`auth.get_access_token()`); no worker production calls on submit.

## Pagination and `--since`

`limit` 1..25 (default 25), `after` task id, `project/task/host` filters,
`since` as `<N>m|h|d` or ISO-8601. `since` filters available timestamped rows
only. `categories.diff` on GET is always `not_available_on_get`
(`use_POST_/api/status_to_project_and_advance_caller_baseline`).

## Complete projection with durable caller baseline (`POST /api/status`)

One authenticated `POST /api/status?project&task&host&since` (empty JSON
body; `limit`/`after` are rejected with `pagination_not_supported_on_post`)
returns the complete bounded summary for ALL authorized matching tasks (up
to the 256-task intake cap): full per-task DTOs (same bounded shape as
GET), `summary: {total, by_status, capped_at, truncated}`,
`pagination: {next: null, ...}`, plus a short durable diff since the
caller's previous POST, all in exactly one call total.

Baselines live in the additive `status_baselines` table keyed by caller
identity (`family:…` / `human-family:…` / actor) plus the normalized
filter tuple, so different callers and different views never share
baselines. Stored snapshots are compact per-task digests
`{v, u, s, h}` (version, updated_at, status, session-digest) — full task
histories and bodies are never copied. Diff is id-only:
`{state: first_baseline|available, added[], removed[], changed:
[{id, fields: [version|updated_at|status|sessions]}], baseline_advanced:
true}` (arrays bounded at 256). Canonical task state is never mutated and
`intake_meta.revision` is untouched; only `status_baselines` is written
(oldest rows trimmed past 5000). Cookie callers pass the existing session
CSRF, like every other POST.

## CLI

`hub status [--since 2h] [--json] [--task ID] [--project P] [--host M]
[--after ID] [--limit N] [--local-only]`

One CLI invocation makes exactly one `/api/status` request using the
existing token/device auth. By default it POSTs (complete summary + own
baseline diff); `--limit`/`--after` stay on the readonly paginated GET,
which never advances the baseline. `--local-only` preserves the old
local-config diagnostic without any API call. Human output escapes
terminal controls; `--json` prints the raw DTO.
