# Proposal: Developer handles trivial configuration fixes directly

**Disposition: PROPOSAL only — not accepted, not integrated.**
Until the owner reviews and accepts it, this file records the proposal and
changes no normative team process. `AGENTS.md` and `docs/` process rules are
unchanged by this document. Intake `6dc5088d-79e7-4a1e-bc43-fe935392511c`
is the proposal record.

## Origin

Filed as process-change proposal (a) from the first-try-fix-failure RCA,
intake `ef2e10b0-1e4a-4350-8e39-24f56d7d512b`. Triggering example (Alexey,
t1683u–t1686u, 2026-10-27): the trivial Codex quota-reserve correction
(intake `c269353c`, reserve `15%` → `0%`) failed twice through the normal
fixer-intake + fresh-session path. RCA evidence:
`/home/alexey/tmp/agent-reports/first-try-failures-2026-10-27.md` (PARTIAL;
intake `ef2e10b0` remains OPEN). Related fixer intakes: `488b860e`
(launcher/`dispatch_local_command_failed`), `7db967f6` (sends stuck in
recorded), `28302c74` (zcodex goal capability).

Thesis: routing a one-line, reversible, obviously-correct config correction
through a new fixer intake plus a fresh headless session adds two extra
first-attempt failure surfaces (dispatch/launch, no-report exit) for zero
safety benefit. The Developer already holding the parent intake should make
the correction directly, with an audit note and targeted verification.

## 1. Objective criteria for a trivial config fix

ALL of the positive criteria must hold, and NONE of the exclusions may hold.
Any doubt disqualifies: file a normal fixer intake instead.

Positive (all required):

1. Single configuration value in a single tracked file (e.g. a numeric
   floor, timeout, retry count, feature flag, quota-reserve constant).
2. No logic, schema, API, or dependency change; no new files except the
   corrected value itself.
3. Fully reversible with one `git revert` (no migration, no data rewrite,
   no side effects outside the repo).
4. Bounded blast radius: affects only dev/test/local behavior or a
   non-production constant; the corrected value is stated verbatim in the
   parent intake AC or the RCA.
5. Verifiable by an existing targeted test or a single command whose output
   shows the corrected value.

Hard exclusions (any one disqualifies):

1. Secrets: credentials, tokens, keys, `.dev.vars`, host.json, or any
   credential file.
2. Access controls: auth/authn/authz, permissions, enrollment, pairing,
   allowlists, security boundaries.
3. Deployments: release plumbing, CI deploy jobs, worker routes/bindings,
   migrations, production enrollment or promotion.
4. Data loss: deletions, retention purges, database wipes/rewrites,
   anything not restorable by revert.
5. Production changes: anything affecting production traffic, data, users,
   billing, quotas upward, or money movement.

## 2. Who, review/notification, minimum verification

- Who: the Developer session already assigned to the parent intake. No new
  fixer intake is filed and no fresh session is launched for the correction.
- Notification: a brief audit note is appended to the parent intake before
  or with the fix: old value → new value, why, the exact revert command,
  and the verification output. The note cites the parent intake and
  `ef2e10b0`.
- Review: post-hoc, not pre-approval. The correction rides the normal
  branch → PR → CI full suite → merge path; the head/owner reviews it as
  part of that path exactly like any other commit. Direct push to `main`
  is still forbidden; deploy gates still require Alexey.
- Minimum targeted verification: run the repo's targeted pre-push lane
  (`scripts/test-affected-local.sh` via `scripts/capped-test-retry.sh`)
  before pushing, plus the CI full suite green on the PR, plus one direct
  functional check (command output or targeted test) showing the corrected
  value in effect. Failures are fixed forward by the pusher; a red CI full
  suite is never merged over.

## 3. Adoption status

This rule is NOT in force. Adoption requires explicit owner review and
acceptance, at which point the accepted text is added to the team process
documentation (`AGENTS.md` or the designated `docs/` process page) in a
separate change. Until then, this proposal file plus intake `6dc5088d`
are the entire record.
