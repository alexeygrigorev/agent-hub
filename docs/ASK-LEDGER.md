# Ask ledger (intake 55c10deb)

Alexey's asks kept getting dropped. Each fix so far was a prose rule that only works if an agent remembers it. The ask ledger makes the Hub see his raw words, so a dropped ask raises an alert instead of waiting for him to notice.

## Flow

1. **Record.** Every message lands in `ask_messages` verbatim, both user and assistant sides.
   - The primary writer is the Tabo watcher (intake 290b935a), which reads Grok Bot replica blobs. It does not spend bot tokens.
   - The fallback is `hub asks record --ref tNNNNu --agent <bot> [--intake <id>] --text-file -`, or MCP `asks_record`.
   - Both are idempotent on (agent_id, msg_id). Text is never trimmed, rewritten or truncated: over-long items are rejected.
2. **Split.** Each user message becomes one or more asks.
   - Rules-v1: list items, else paragraphs, else the whole message.
   - `AskHooks.split` can plug in a cheap LLM. Its output is accepted only when every part is a verbatim slice of the message.
   - Bare acknowledgements ("ок", "thanks") are recorded as `not_an_ask`.
3. **Map.** Each ask is mapped to an intake and an AC line, in this order:
   - an explicit `intake_id`;
   - an intake whose title, AC or history cites the message ref (`tNNNNu`);
   - `hub asks map <ask> --intake <id> [--ac N]`.

   Within an intake, the AC line is the one citing the ref, else the line with at least 50% word overlap, else none.
4. **Reconciler (290b935a A10).**
   - When a batch brings new user asks, the 5-minute cron starts one headless reconciler session 2 minutes after the last such batch. The engine follows routing policy.
   - The reconciler maps each ask, adds a verbatim AC line, files a missing intake, or dismisses the ask with a reason.
   - It never runs two at a time.
   - Turn it on with `hub asks settings --reconcile-intake <id>`.
5. **Watchdog tick** (existing 30-minute IntakeWatchdog):
   - An ask still unmapped 30 minutes after it arrived becomes `unmapped`. It is listed in one aggregated `asks_unmapped` alert per tick, once per ask. Mapping every ask in that batch resolves the alert.
   - An intake that closed done without proving a mapped ask makes that ask `dropped_after_done`, alerted once as `asks_dropped`, and the intake is reopened with a note. Backfilled asks (source `backfill`) are flagged in the ledger state only: they never page, reopen intakes or start the reconciler.
   - **Gaps** (`asks_unrecorded`): message ranges that never reached the ledger. A replica window that starts past everything seen before (`covered_through`), or a watcher-reported `gap`, is a `window_overflow`. Each range is alerted once; holes close when they are filled.
   - **Silent source** (`asks_source_silent`): the watcher has not posted for more than 2 hours while its host heartbeat is fresh. The alert resolves on the next ingest.
   - **Daily** (`asks_agent_silent`): an agent whose chat window advanced in the last 24 hours, with zero user messages recorded.
   - Counts appear in `hub status` (`asks`), in the daily intake digest, and on `/asks`.
6. **Close gate (AC5).** This sits beside DONE evidence and the worktree gate in `IntakeService.transition`, and is the hook that done-gate f0b21a52 shares.
   - `status=done` is refused with `asks_unverified` while any mapped ask is unproven.
   - A mapped ask is proven when a DONE note in the closing context marks it, in one of these forms:
     - `AC<n>: met`
     - `<tNNNNu>: met`
     - `ask <8-char id>: verified`
     - a Hub verifier PASS note covers AC-mapped asks
   - An ask can also be proven explicitly with `hub asks verify <ask> --evidence TEXT`.
   - A "not met" mark beats "met".
   - The operator's `closed_unverified` close is not refused; its asks are marked `closed_unverified`.

## Gap scope

The `tNNNN` counter also numbers non-user entries: `t1985u` is a user message and `t1985s1` is an assistant send. Holes in user ids are therefore normal. The default `gap_scope=window` detects only window overflow. `agent` and `global` detect user-id holes per agent or across all agents (`hub asks settings --gap-scope`), for sources that number only user messages.

## API

| Route | Who | What |
|---|---|---|
| `POST /api/asks/ingest` | host bearer (own machine) or operator | contract v2 agreed with 290b935a; per-item results `{batch_id, accepted, accepted_user, duplicates, conflicts, rejected, gaps}` |
| `GET /api/asks/cursor?agent_id=` | host or operator | resume cursor, `covered_through`, `window_diag` |
| `GET /api/asks?state=&agent_id=&intake=` | operator | asks with ref, intake + AC line, state |
| `GET /api/asks/messages`, `/api/asks/messages.jsonl?after=&since=&agent_id=&role=&q=` | operator | both sides; JSONL has `ts_utc` and `ts_berlin` |
| `POST /api/asks/:id/map\|dismiss\|verify` | operator | mapping and resolution |
| `GET /api/asks/summary`, `POST /api/asks/check`, `POST /api/asks/reconcile`, `POST /api/asks/settings` | operator | maintenance |

## What still relies on discipline

- **The writer must run.** The watcher is the writer now. Its silence is alerted, and missing ranges are alerted through window overflow. A bot that is not covered by any watcher and never calls `asks record` is invisible.
- **Split quality.** Rules-v1 is deterministic and keeps every part verbatim. No LLM split is wired by default: the Worker has no AI binding, and adding one needs an operator decision about the wrangler config and the test environment. The reconciler session does the semantic mapping.
- **Mapping and AC quality.** Whether the mapped AC line actually satisfies the ask is judged by the reconciler and the done-gate verifier, not by the ledger.
- **Dismissals and manual verifies.** These are operator-visible escape hatches with a required reason or evidence.
