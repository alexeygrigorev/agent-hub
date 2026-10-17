# Hub Operations Handoff (generic runbook)

Scope: carry remaining alert-default follow-up obligations to current evidence
+ reviewable activation plan. Strictly readonly until head ACKs an exact
activation step. No deploy, DNS, custom-hostname, AWS, enrollment, pairing,
credential, service, timer, or native-probe changes without that gate.

## 1. Preflight (every run)

- Verify isolated worktree HEAD + clean status (untracked cache symlink only).
- Confirm alert-default ancestor present; never recommit landed commits.
- Check resource budget: total owned `.local` + `.wrangler` + `dist` under
  early/hard limits before any test. Use only the head-guarded runner with
  explicit `--label`, `--early-mib`, `--hard-mib`, `--timeout`,
  `--retire-child-tmp`. Record real peaks (apparent/allocated/child RSS,
  threads, cgroup, max sample gap, exit). Guard abort = failure, never green.
- Never execute legacy full-suite ship scripts. Old pipelines without
  `pipefail` hide failures (`vitest | tail` exits as `tail`); always run
  unpiped with honest propagation.

## 2. Bounded liveness reproducer (max 2 selected cases)

- Use a dedicated vitest config that reuses actual app source + local SQLite
  (no mirrored fake-green implementations, no weakened assertions).
- Suggested pair: (a) default-inbox delivery when alert target unset
  (exercises Durable Object `runChecks` + notify path), (b) stale-alert open /
  resolve (pure liveness evaluate/reconcile, no network).
- Run via head runner, ONE guarded invocation, explicit `-t` filter matching
  ≤2 cases. Separate outcomes: real assertion failure vs setup/authoring
  failure vs resource abort vs harness internal error. Record stdout counts
  + stderr reason codes only (sanitized, no tokens/env/bindings).

## 3. Deployed revision reconciliation (readonly)

- Confirm custom hostnames resolve (or record unresolved with errno only).
- Confirm existing `workers.dev` fallback resolves via DNS; then ONE plain
  unauthenticated HTTPS GET to a public auth-gated endpoint (e.g.
  `/api/liveness`) expecting `401`. Status code alone proves the endpoint responded; it does
  not identify the serving app, its revision, or authenticated capability.
  Attribute app behavior only with matching response/source evidence.
- Authenticated revision/environment/capability reads (token verify, account
  capability) only in memory, only against the verified target account/Worker,
  never with token in cmdline/stdout/history; reject redirects to foreign
  hosts; never publish account lists. Credential file paths are pointers
  only — never cat/source/dump/rotate from ops slices.

## 4. Domain / token readiness (readonly)

- Token zone scopes unknown → capability check without dumping credentials.
- If zone capability truly absent, record minimal human action (what scope is
  needed, no secret/code exposure). A valid `workers.dev` endpoint may unblock
  the functional loop before custom DNS; never invent reachability/deploy.

## 5. Host / pairing / service-cause inventory (readonly)

- Local metadata only: CLI `--help`/`--version`, file paths, hashes, profile
  names (no env dumps). Verify CLI offers start/send/capture/stop + intake /
  handoff / status verbs.
- Fleet service failed-exit + timer-active with unknown cause → record reason
  code, do NOT just restart. Preserve old timers until replacement is observed
  working; migrate one at a time with observed-replacement checks.
- Protected principals / stood-down roots: never start root/principal/foreign
  sessions or send busy-composer input. Pairing/enrollment stays head-gated
  with predecessor-custody confirmation.

## 6. Activation gating

- Runtime activation needs an EXACT head-ACKed step after independent source
  acceptance, custody, budget and qualification proofs. Read-only diagnosis and
  bounded source-review tests proceed within their already declared authority;
  they do not require source acceptance before they can establish it.
- User handoff authorizes continuing work; do not re-ask for generic
  confirmation. Send candidate action + evidence while continuing independent
  readonly work.
- Source fixes (if needed): request exact path/symptom/proposed small fix;
  continue readonly matrix while waiting. Independent different-provider
  review before main. Commits use explicit owned paths, `[skip ci]` + agent
  trailer, git-lock/index-empty guard; no main push/deploy from workers.

## 7. Logging / sanitization

- Logs/captures: bounded reason codes, sources, revision IDs, host capability
  facts only. Never tokens, cookies, raw env, credential contents, binding
  secrets, or private prompt scrollback.
- Incremental verified artifacts (receipts, configs, runbook updates), not
  intent-only summaries. If blocked on review/token, send completed preflight
  + next-action owner; keep task OPEN, not done.

## 8. Authenticated readonly discovery pattern (approved scope only)

- Credentials live in memory only: parse local credential files without
  printing them (key names + presence booleans at most), build auth headers
  in memory, never pass secrets via cmdline/env/stdout/history. Credential
  file paths are pointers, never sources to dump, copy, or rotate.
- Fixed trusted endpoints only (Cloudflare API host, the one existing Hub
  hostname). Verify hostname + account/worker binding before each call;
  follow same-host HTTPS redirects only, abort with a reason code on any
  foreign redirect. GET only, bounded timeouts, no retries/sleeps.
- Receipts carry IDs/timestamps/status/reason codes/hashes/qualification
  facts only: token-active booleans, HTTP statuses, provider error codes,
  deployment/version IDs + created_on + GitSHA-or-UNKNOWN (never guess a
  commit from main), zone read counts + statuses, inventory counts + labels
  (hash workspaces/paths), service ActiveState/SubState/Result strings.
- Interpret with the source and observed response shape. Unauthenticated 401
  establishes an endpoint response; unexpected empty/non-JSON 403 cannot by
  itself establish an app scope denial or invalid credential. Match app error
  codes/content before attributing it; keep perimeter failures as unknown. A
  token-verify failure alongside successful scoped reads does not prove
  revocation; verify token kind and endpoint requirements separately. A zero
  zone result is limited to the queried account and token visibility. Successful
  zone/domain/Worker READs never prove write/deploy/DNS permission. OAuth/token refresh, login flows, and
  sign-in endpoints are credential operations — never run them under a
  readonly grant; record as blocked-with-proof + minimal next action instead.
- Absence is evidence: unresolved DNS (with errno), zero zone results,
  placeholder client configs, inactive units, and empty failure lists are all
  recorded as observed facts that narrow where the real state lives.
- Inspect services at the right scope and exact unit name: user daemons live
  under `systemctl --user`, and similarly-named units (e.g. a host service
  vs a fleet-host service) are different things — a wrong-scope/wrong-unit
  read proves nothing about the real unit. Parse `show` output with a
  sanitizer (allowlisted properties + `--config` path only, never
  ExecStart/env values), and treat a source-emitted generic failure string
  as UNKNOWN root cause until corroborated, never as proof of one hypothesis.
