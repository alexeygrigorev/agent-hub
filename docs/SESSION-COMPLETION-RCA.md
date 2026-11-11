# Session completion RCA and Hub-owned completion control

Intake `cb8a6eff` (Alexey, 2026-11-11, t3130u–t3134u):

- t3130u «давай сделаем анализ почему это случается - я что-то прошу но мне потом приходятся просить еще раз»
- t3131u «ну ты сам и не должен доводить»
- t3132u «но когда ты начинаешь сессию по задаче я ожидаю что она завершится сделанной задачей»
- t3134u «я хотел бы чтобы хаб это мог контроллировать»

Question: why do started sessions end with acceptance criteria (AC) unmet, with no
autonomous recovery, and with results undelivered until Alexey asks again?
Every cause below is observed in code at `origin/main` 9fcd1e4a and in Hub
intake audit records, not inferred from status labels. Audit `seq` numbers
refer to `hub intake audit <id>`. Times are UTC (Europe/Berlin = UTC+1).

## Verified code causes (before this change)

| # | Cause | Code |
|---|---|---|
| C0 | The session never sees the AC or a way to report completion. The host receives `acceptance_criteria` in the offer but launches the native engine with `launch.prompt` only. | `hub2/src/dispatch.ts:22` carries AC to the host; `hub2/host/agent_hub_host/native_launch.py` `launch()` builds the goal from `specification["prompt"]` only |
| C1 | Task outcome = process exit code. `exit_code===0 && !launch_failed && !deadline` gives `completed-awaiting-review`; anything else gives `failed`. AC and worker notes are never read. | `hub2/src/project-do.ts` `finishInternal` (`finishedStatus`) |
| C2 | No autonomous recovery. A terminal attempt sets the intake `failed` and stops. `retryableFailure` only *permits* a manual restart; dispatch retries cover only pre-native launch (≤120 s). Nothing in hub2 schedules a successor. The Hub1 watchdog/forgotten path was not ported: `hub intake forgotten` returns `HTTP 404 not_found` against hub2 (verified 2026-11-11). | `project-do.ts` `failDispatch`, `deferSaga`, `retryableFailure` |
| C3 | No delivery. The finish path updates the intake row and appends events only; no event records the terminal status itself, and nothing is sent to the requester. The mailbox transport (`MailboxDO`, human recipients, receipts) existed but was never invoked on finish. | `project-do.ts` `finishInternal`, `stoppedInternal` |
| C4 | Default `timeout_seconds` is 1800. Long tasks are killed at the deadline (exit 124, or −1 via signal 1) mid-work. With C2 this turns a "needs more time" into a dead task. | `project-do.ts` `prepareStartIntake` default; host `--timeout-secs` |

Missing pieces, by kind:
- **Event:** there is no task-outcome event, only process-exit evidence.
- **Recovery:** there is no Hub-owned continuation.
- **Transport:** there is no outcome delivery.

## Representative cases (verified from audit records)

All 24 `session_start_requested` events across these intakes carry a `human:` actor (the operator credential that both Alexey and the orchestrator use). `hub2-dispatch`/`machine:*` only stage, offer, claim, start or fail. No Hub-initiated restart exists in any case.

**Media kit `02bed9f2`.** Request 11-10 17:36. 81f9dc7a launch failed `internal_error` (seq 3–6). 3de05f53 launch failed `provider_refresh_invalid_grant` 11-11 02:16 (seq 14–17). Then **17 h with no start** until the note recording t3128u (seq 18, 19:22). 4530e5ec failed `workspace_label_unavailable` (seq 20–23). b8096ef6 started 19:24 (20:24 Berlin) and exited 0 after 260 s, giving `completed-awaiting-review`. Separately, an unmanaged native 6b9269de ran in the worktree with last activity 02:32 (03:32 Berlin); no Hub session linked it. The "revoked token" explanation appears only in a note (seq 29), so it is a label, not an observed event. Gaps: C2, C3.

**Finance `49d43130`.** All three runs hit the 1800 s deadline:

| Session | Requested | Exit |
|---|---|---|
| 164ffd25 | 17:40 | 124 at 18:10 (19:10 Berlin), seq 10 |
| 3be0d643 | 18:37, after reopen | 124 at 19:07, seq 20 |
| 4fda34a3 | 19:21, after t3129u reopen | 124 at 19:51, seq 29 |

4fda34a3 had posted "DONE … PR #413 merged … deploy succeeded" (seq 28) and still ended `failed`. Exit code overrode the evidence in the "done but failed" direction. Gaps: C1, C4, C2.

**GitHub fixes, restarted only on the t3129u repeat (19:21–19:24), with reopen notes citing t3129u before each start:**

| Session | Intake | Exit | Notes in the run | Hub status |
|---|---|---|---|---|
| 9fae1565 (templates) | cdb7e0b4 | −1 at deadline | none | failed |
| 47d58acc (delete) | f41f6f0c | 0 after 124 s | "DONE (live)" | completed-awaiting-review |
| e9bd4ab7 (E2E) | ac4284cf | 0 after 27 min | **none** | completed-awaiting-review |
| 478490d0 (W01–W13) | 007b8aba | 0 after **53 s** | **none** | completed-awaiting-review |

Earlier attempts on the same intakes:
- **cdb7e0b4:** 441aca14 wrote "HANDOFF… NOT DONE" (seq 19), exited 0, and became `completed-awaiting-review` (seq 20). Exit code overrode the evidence in the "not done but completed" direction.
- **f41f6f0c:** 036b7ba7 exited 0 after 48 s with no notes or PR change and became `completed-awaiting-review` (seq 21). It was reopened only manually (seq 22).

Gaps: C1, C2, C3.

**Delivery versus opening-only replies.** No intake record in these cases shows any outcome message to the requester (C3). Workers wrote "MANDATORY SUCCESSOR" / "HANDOFF (1800s budget may time out)" notes. Nothing consumed them, so a successor waited for a human turn.

**Process exit versus task completion.** The exit code is wrong in both directions: exit 0 with nothing done (036b7ba7, 478490d0, 441aca14) and exit 124 with the task done (4fda34a3). Status labels are therefore not evidence of completion.

## Written-rule contradictions (bots-management, verified on disk)

- `references/supervising.md:6`: "Resume supervision on a completion/failure signal". hub2 emitted no such signal (C3), so supervision resumed only when Alexey asked.
- `references/intake-and-ac.md:101`: "On every drain, check for forgotten intakes and restart them". The tool behind it, `hub intake forgotten`, 404s on hub2 (C2).
- `intake-and-ac.md:86`: "until Hub auto-resume lands, the drain routine and orchestrator do this". Hub auto-resume never landed in hub2, while the no-polls/«все рутины через хаб» rules retire drains. Recovery was assigned to mechanisms that were retired or forbidden.
- Stale polling references remain: `intake-and-ac.md:24` (6h "Open intake drain" routine), `:57` ("check progress at least daily"), `:82` ("Routine Pending research follow-ups polls this file"), `:102` ("sweep agent OK"), and `supervising.md:44` ("Routine/drain checks of a running goal session").

The reflection already committed on bots-management main as 667f85d is not re-edited here. With Hub-owned completion, the rules can point at Hub's own signal instead of drains.

## Hub-owned completion control (this change)

The fix is event-driven inside `ProjectDO`, the same object that owns every
start and terminal receipt. It adds no schema change, no recurring watcher, no
parent poll and no new start gate.

1. **Contract (fixes C0).** A non-shell, non-scheduled start appends a completion contract to the prompt, kept within the Machine's 12 KB launch limit. The contract states:
   - the intake and Hub session ids
   - that process exit is not completion
   - the AC list, clipped with a pointer to `hub intake show` when large
   - the exact claim command: `hub intake append <intake> --event-file` with `{"kind":"task_complete","session_id":…,"acceptance":[{"status":"met","proof":…}×N]}`

   The saga is marked `completion_contract` in its server-only routing record. If the contract cannot fit alongside an otherwise valid launch, Hub preserves the exact original prompt and marks `completion_contract_omitted: "launch_budget"`. The task stays outcome-controlled: a valid explicit claim can finish it, but an absent claim produces an unfinished terminal delivery without an automatic child. Auth fallback preserves both markers. Sagas started before the deploy keep the old semantics.
2. **Outcome (fixes C1).** At finish, or at a non-launch stop, a controlled attempt is `completed-awaiting-review` only if a claim exists that is bound to that exact session and has one non-empty `met` proof per criterion, whatever the exit code. Otherwise the intake stays `failed` (unfinished). An event records it: `task_completion_claimed` or `task_unfinished` with the reason (`exit_without_completion_claim`, `deadline_exceeded`, `native_exit_<n>`, `session_dead|idle|…`, `launch_failed`). Operator review/acceptance is unchanged.
3. **Recovery (fixes C2, C4).** An unfinished exit (nonzero, deadline, exit 0 without claim, or a reaped `dead`/`idle` process) gets a continuation from Hub through the canonical `startIntake` path, under the internal actor `hub2-completion`:
   - **Route:** session cap and picker apply. The continuation keeps the same host, accepted absolute workspace binding, engine, model, effort, goal and interactive mode. A mutable workspace label is never resolved again after the original attempt was bound.
   - **Prompt:** a continuation preamble (read `hub intake history`, do not redo finished steps) plus the original task, and a fresh contract bound to the new session.
   - **Timeout:** doubled after a deadline exit, capped at 7200 s.
   - **Bound:** at most 2 consecutive continuations per requester start, counted from the start events' actors.
   - **Refusals:** a refused start retries with backoff (1 m, 4 m, up to 3 tries). After that, the task is reported.
   - **No duplicates:** a requester restart supersedes a pending continuation. Every post-await action write compares its original intake snapshot, including session, status, update version and evidence; an old failure cannot plant a retry into a newer claimed attempt.
   - **No loop:** launch refusals, lost hosts and operator stops are not continued. A pending STOP recorded by the Machine also prevents a child when qualified native finish wins the ACK race. The Machine freezes this intent in its durable finish outbox; it is not a STOP acknowledgement or confirmation, and host-supplied flags cannot create it. Retries keep the same finish body even if the command state changes later.
4. **Delivery (fixes C3).** Every terminal outcome (claimed; unfinished after the bound; refused; stopped; launch failure) sends one message to the intake owner's Hub mailbox. The sender is `intake:<id>` and the idempotency key is `terminal:<operation>`. The body names the outcome, the session, the claim summary, and the next command (`hub intake history`, accept, or restart). Transient failures retry with backoff, up to 5 times. The result is recorded as `task_outcome_delivered` or `task_outcome_delivery_failed` with the message id.

Pending actions live in the intake's own `final_evidence.completion` and do not expire with intake age. The existing project alarm and its bounded eight-row drain use the same eligibility predicate: terminal intake, current controlled session and current marked saga. A pending continuation's deadline takes priority; otherwise a pending delivery supplies the deadline. Eligibility is applied before the row limit and alarm minimum, so stale metadata cannot hide work or spin the alarm. Each action is CAS-guarded against the attempt that created it. No new table or watcher is added.

### Known limits

- The project cannot tell a reaper idle-stop of a live interactive session (`stop_confirmed` issued by `hub2-session-reaper`) from an operator stop. Both are reported, not continued. Telling them apart needs the stop actor in the Machine receipt.
- A claim counts only for the session it names, only if written after that session's start, and only if written by that session's own scoped credential (`session:<id>`) or an `operator:admin` actor. Native sessions currently append under the host's operator CLI profile (`HUB_CONFIG_FILE`/`cli.json`), because the maintained `hub` CLI does not read the per-session `HUB2_CONFIG` worker credential. So in practice the binding is the session id in the contract plus ordering. Wiring the CLI to the worker credential would make claims session-authenticated.
- Host-supplied finish evidence can never carry a completion record. Hub acts only on records for the intake's current controlled attempt.
- Every controlled terminal path delivers, including auth-fallback exhaustion and legacy unobserved launches. A continuation keeps the original requester as the assigned actor and is fenced to the attempt it continues.
- Launch-start failures stay with their existing owners: 06d135ab (explicit-model start `internal_error`) and 8faad246 / 217342ab (Windows `native_launch_unverified`). abf8e6d2 covers the Hub usage audit and recurring start failures. Hub now reports these failures to the owner instead of letting them sit silently.

- Engines receive the prompt as a positional argument (`claude -p … <prompt>`, built by the aplexer task runner outside this repo). A prompt that starts with `-` is parsed as an option, and the engine exits 1 before the task starts. The first live probe (intake 0acaad70) caught this: the continuation preamble began with `--- Hub continuation ---`, both continuations failed with `error: unknown option`, and Hub hit the bound and delivered "NOT finished". Hub-generated prompts now never start with `-`. A user brief that starts with `-` is still exposed until the runner passes `--` before the prompt; that is a follow-up for the runner owner.

### Linked owners (not duplicated)

- **03ca4ba7 / b38954eb:** `--notify=orin` completion notify. A launcher-level notify. This change delivers inside Hub to the intake owner.
- **8faad246 / 217342ab:** Windows `native_launch_unverified`.
- **abf8e6d2:** Hub usage audit and recurring start failures.
- **a772fe35 / eb64f304:** Fable reliable launch.
- **4b8e6357:** Fable plan orchestrator.
- **06d135ab:** explicit-model Fable start `internal_error`.
- **baab076a / b56f13fe:** goal placement filter. Explains the `launcher_capability_unavailable` restarts of this very intake.

### Tests

`hub2/tests/task-completion.test.ts` runs in CI's project-intake semantic step on the production Node SQLite storage. It covers:
- contract injection and size bounds
- false completion (exit 0, NOT-DONE note, foreign and partial claims), followed by an automatic continuation
- a claimed task that times out, which stays finished and is delivered once, with retry after a transient mailbox failure, surviving a restart
- the continuation bound, the doubled deadline timeout, exhaustion delivery, and the reset on a requester start
- refused continuations with backoff, then delivery; the related unexpected-error exhaustion path also schedules terminal delivery, surviving a restart and mailbox retry without duplication
- operator stop and launch refusal reported but not continued; a dead reap continued
- a requester restart superseding a pending continuation
- continuation and delivery after a restart and an outage longer than 25 hours, with ineligible records filtered before the eight-row limit and alarm minimum
- accepted workspace binding reuse, and late async failures that cannot overwrite a newer claimed manual attempt
- valid quote-heavy launches with omitted contracts, explicit claims and auth fallback retaining outcome control
- actual Machine-to-Project pending STOP/finish races, lost-forward-response replay, unchanged STOP confirmation policy and rejection of host-forged intent
