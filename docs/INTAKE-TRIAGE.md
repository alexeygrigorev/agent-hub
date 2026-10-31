# Backlog triage, post-DONE verification and human-hold alerts

Intake d8d509d2 (AC2, AC5, AC8; Alexey t1936u–t1937u). All three run from the existing `*/30` scheduler slot.

## Post-DONE verifier (`src/intake-verifier.ts`)

When an intake's work reports DONE, Hub decides whether a separate verifier agent must check it. Each intake has a `verification` field (`hub intake create|update --verification required|skip|auto`, default `auto`).

`auto` rules:

- **Required** if the change touches prod (deploy, DB, migrations, env/infra), money or payments, auth/secrets/permissions, or sends to real people (email, notifications, messages).
- **Required** if it deletes or cleans data or resources, or touches Hub core (queue, placement, sessions, engine accounts, fleet).
- **Required** if the intake itself asks for verification.
- **Skip** for docs, reports and research, small UI or copy edits, tests/CI-only, and dev-only changes.
- **Otherwise** the changed-file paths in the DONE note decide: Hub core paths mean required; docs/tests-only paths mean skip.
- **Still unclear** means required.

Each run:

1. Takes every open intake held for a completion claim whose newest DONE note is newer than the verifier's first enablement. Older holds stay with the operator. Verifier verdicts and backlog-triage notes never count as claims.
2. Records the decision and its reason in `intake_verifications`.
3. **Skip:** appends `VERIFICATION SKIPPED — <reason>` and closes the intake on its DONE evidence. A skip needs a DONE note with sha, live check and result; without one, the claim is verified instead.
4. **Required:** starts one headless verification-only session through the canonical session start. It is linked to the intake, uses `tier: review`, and may run on the same model as the implementer (Claude Opus by default; no forced engine switch). At most 3 verifiers start per run. The start is detached (`intake_worktree: "detached"`, intake 79458f2d): it runs in the project's workspace root, never in an intake `-wt-` path, so a live owner session holding the intake worktree lock never blocks it.
5. **Pass** (the verifier's note starts `DONE — VERIFIED by Hub verifier` with evidence, live check and result lines): Hub closes the intake through the ordinary close rules (DONE evidence plus the worktree close gate).
6. **Fail** (`VERIFICATION FAILED — …` with a `gap:` line): Hub appends `Verification gap (...)` to the AC. That starts a new context, so the DONE claim stops holding the intake and the watchdog re-queues the work.
7. A verifier that ends without a verdict is retried once and then escalated. A blocked close (worktree gate), a start refused for missing AC or scope, or a verifier session that was refused or failed before native start (a pre-start `launch_error`, for example `dispatch_worktree_refused`; a verifier that ran and failed at runtime is retried as usual) is also escalated, once, as an `intake_verifier` alert. A failed start is never retried silently. A verifier still `held` or `queued` for placement after one hour raises one `intake_verifier` alert (subject `<intake>:verification-held`, reason `reviewer_held:<hold reason>`) and keeps waiting; it is never relaunched or cancelled for that.

## Hourly backlog triage (`src/intake-triage.ts`)

Each run takes the forgotten intakes that have no triage verdict yet, oldest first. A verdict is a note starting `TRIAGE ACTUAL|DONE|OBSOLETE|DUPLICATE`, or a `DONE` note mentioning `backlog triage`. Completion claims are skipped, because the verifier owns them.

- One headless Claude Opus reviewer runs at a time (`engine: claude`, `tier: review`, workspace `agent-hub`). It is linked to the triage intake d8d509d2, because headless starts must link an open intake. Its brief carries 10 intakes.
- The start is detached from d8d509d2's worktree (`intake_worktree: "detached"`, intake 79458f2d). It runs in the `agent-hub` workspace root and never claims or locks an intake worktree, so live owner sessions on `agent-hub-wt-d8d509d2` never block triage.
- A refused start (any non-2xx reply, including a 400 that carries a `refused` session id) or a reviewer session that was refused or failed before native start (a pre-start `launch_error`; runtime/first-action failures of a session that started do not count) raises one `intake_triage` alert (subject `backlog-triage-start`) per distinct reason. A failed session's slice is offered again next hour. The alert resolves once a reviewer reaches `running` or ends normally. A reviewer still `held` or `queued` for placement after one hour (for example during a Claude dispatch pause) still owns its slice: Hub never starts a duplicate and never cancels it. It raises the same alert once with reason `reviewer_held:<hold reason>`, which resolves when that reviewer runs or leaves the active states. `hub intake forgotten` shows the open reason as `triage.start_alert`.
- The reviewer appends one verdict per intake, writes missing AC from the verbatim intake text, and corrects wrong projects.
- An intake is offered again only after 24 h without a verdict, or after an explicit admission refusal (4xx). An unknown outcome (5xx) keeps the hold, so a lost reply never starts a duplicate reviewer.
- Nothing is closed, cancelled or deleted by triage.
- The verdict gates the backlog idle kick (intake 7736fdaa). The idle kick starts an intake outside the continuous-pickup scope only if its newest verdict is `TRIAGE ACTUAL`. Untriaged intakes of any age are skipped (`idle_kick_untriaged`), and so are intakes whose newest verdict is OBSOLETE, DUPLICATE or DONE (`idle_kick_triage_<verdict>`). See [ORCHESTRATOR-LIVENESS.md](ORCHESTRATOR-LIVENESS.md).
- Reviewer access: the reviewer's worker credential may read, add notes to, and set `project` and missing `acceptance_criteria` on exactly the intakes in its open request ledger rows. It can never change status or title, and never replace existing AC. Access to an intake ends as soon as a verdict note lands on it, when the request leaves `requested`, or when the reviewer session ends.

## Human-needed holds alert once (`src/intake-watchdog.ts`)

Watchdog holds that only a person can clear are each alerted once per (intake, reason) pair, in one aggregated `intake_human_hold` alert per run: workspace configuration, scope decisions, and completion reviews not owned by the verifier. The alert is recorded in `intake_human_alerts` and in the intake audit.

## Status

`hub intake forgotten` includes `triage` and `verifier`. The scheduler result carries `intake_triage`, `intake_verifier` and the watchdog's `human_hold_alerts`.

## Operator close without evidence (AC9)

`hub intake close ID --outcome closed_unverified --reason "..."` closes an intake that has no DONE evidence. It exists for operator-ordered bulk closes of old tasks.

- It is operator only and requires a reason.
- It records `outcome: closed_unverified`, the reason and the actor in the intake history. It never invents a SHA or a live check.
- The audit kind is `close_unverified`, never `close`, so it does not count as a verified completion.
- It is idempotent: re-running it on an intake already closed this way changes nothing.
- The worktree close gate still applies (`--force` with the reason overrides it as usual).
- Ordinary closes now record `outcome: completed`.
