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
4. **Required:** starts one headless verification-only session through the canonical session start. It is linked to the intake, uses `tier: review`, and runs on a different model than the implementer: Claude Opus, or Codex when Claude implemented. At most 3 verifiers start per run.
5. **Pass** (the verifier's note starts `DONE — VERIFIED by Hub verifier` with evidence, live check and result lines): Hub closes the intake through the ordinary close rules (DONE evidence plus the worktree close gate).
6. **Fail** (`VERIFICATION FAILED — …` with a `gap:` line): Hub appends `Verification gap (...)` to the AC. That starts a new context, so the DONE claim stops holding the intake and the watchdog re-queues the work.
7. A verifier that ends without a verdict is retried once and then escalated. A blocked close (worktree gate) or a start refused for missing AC or scope is also escalated, once, as an `intake_verifier` alert.

## Hourly backlog triage (`src/intake-triage.ts`)

Each run takes the forgotten intakes that have no triage verdict yet, oldest first. A verdict is a note starting `TRIAGE ACTUAL|DONE|OBSOLETE|DUPLICATE`, or a `DONE` note mentioning `backlog triage`. Completion claims are skipped, because the verifier owns them.

- One headless Claude Opus reviewer runs at a time (`engine: claude`, `tier: review`, workspace `agent-hub`). It is linked to the triage intake d8d509d2, because headless starts must link an open intake. Its brief carries 10 intakes.
- The reviewer appends one verdict per intake, writes missing AC from the verbatim intake text, and corrects wrong projects.
- An intake is offered again only after 24 h without a verdict, or after an explicit admission refusal (4xx). An unknown outcome (5xx) keeps the hold, so a lost reply never starts a duplicate reviewer.
- Nothing is closed, cancelled or deleted by triage.

## Human-needed holds alert once (`src/intake-watchdog.ts`)

Watchdog holds that only a person can clear are each alerted once per (intake, reason) pair, in one aggregated `intake_human_hold` alert per run: workspace configuration, scope decisions, and completion reviews not owned by the verifier. The alert is recorded in `intake_human_alerts` and in the intake audit.

## Status

`hub intake forgotten` includes `triage` and `verifier`. The scheduler result carries `intake_triage`, `intake_verifier` and the watchdog's `human_hold_alerts`.
