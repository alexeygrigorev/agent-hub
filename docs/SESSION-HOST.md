# Session host adapter (M12e, host side) — R2

Owner: session-host worker r2 (`platform-session-host-r2`, actualtask0429).
Scope: `host/agent_hub_host/native_sessions.py`,
`host/agent_hub_host/session_executor.py`, `host/tests/test_session_executor.py`,
this doc. Server queue HTTP, public CLI verbs and server wiring belong to the
API worker (`platform-session-api-r2`); the interface below follows the
head-chosen endpoints. Full signature/result/proof contract for wiring:
`.local/session/HOST-R2-INTERFACE.md` (gitignored working copy).

## What exists

- `native_sessions.py` — reusable native wrappers around the installed `a` CLI
  (argv only, no shell, bounded output, `--pane`/`--no-ack`/`--enter`/`--from`
  rejected at the runner):
  - `list_sessions` (read-only, optional workspace filter, minimal projection;
    verified against real `a list --json`: rows carry `id/tag/workspace/engine/
    phase|state/created_at_ms/last_activity_ms`, never env/secrets),
  - `qualify_target` (tag→UUID+workspace+generation binding immediately before
    an effect; stale `created_at_ms` refuses; phase is liveness only, never an
    idle/readiness claim),
  - `send_inbox` (`message send --to TAG`; state `recorded` at most — inbox
    persistence is never transported/acknowledged),
  - `deliver_recorded` (`message deliver <envelope-id>`; explicit opt-in only;
    maps to `transported`/`refused`, never acknowledged),
  - `fetch_message_log` (bounded exact-workspace `message log`, read-only),
  - `verify_ack_via_reply` (ACK ONLY on a reply envelope in the retrieved log
    with `reply_to` = original native envelope id, `from` session/tag/workspace
    = intended generation, correlation id + body-digest echo; else `unknown`),
  - `capture_output` (bounded tail/screen; `privacy_unknown=True` always),
  - `status_of` (read-only probe), `stop_session` (kill + verified exit with
    UUID+tag+workspace+generation checked before AND after),
  - `resolve_cwd` (workspace-root confinement; validates only — cannot move an
    already-launched cwd, see gap 4).
- `session_executor.py` — durable intents + the existing Fleet `Dispatcher` contract:
  - `start_from_offer` — pure compat helper for the EXISTING Fleet consumer
    only (never polls offers); per-offer lock, idempotency journal,
    reconcile-before-retry via tag adoption. No second consumer added.
  - `send_prompt` (inbox only; `pty-guarded` retired → honest hold),
    `capture_session_output`, `stop_controlled_session`, `session_status`,
  - `CallerCustody` accepts canonical `tag` or `project:tag` actors (bus
    `parseAddress` semantics); family/incarnation/revocation enforced server-side.
  - `handle_claimed_command` — THE shared receiver for API wiring: verifies the
    server claim (token strength/holder/generation/expiry) BEFORE any effect,
    holds the per-command exclusive lock, journals the attempt before the
    effect, runs send/capture/stop for the exact target, journals the outcome,
    and reports `.../ack` with the claim token. `start` rejected (Fleet-owned).

## State model (binding on all layers)

`recorded` (inbox persisted) ≠ `transported` (deliver guard submitted) ≠
`acknowledged` (correlated native reply verified) / `unknown` (no proof — the
honest default) ≠ `uncertain` (reconcile first) ≠ `refused`/`held` (no effect).
Inbox persistence, deliver submission, HTTP success and PTY bytes are never
rendered as target-read/processed. R2 removed the predecessor's
`working:true → acknowledged` mapping: `send --enter` is retired entirely.

## Hard refusals (no bypass)

`--pane`/`--no-ack`/`--enter`/`--from`, busy/non-empty/unknown composers (deliver
guard reports, never typed into), UUID-shaped `--to`, tag reuse, stale native
generation, workspace/cwd escape, over-bound bodies/captures, missing custody,
expired/weak claims, claim/command-id or generation mismatch, repeated tokens
re-authorizing execution, conflicting idempotency payloads, lock contention
(uncertain, reconcile). Unqualified starts are `held` (M6/M8 intact). Absent
records without our kill proof are never exit proof (`forget`/`prune` remove
records without stopping workloads).

## Exact remaining wiring / qualification gaps

1. **Server claim/ack agreement** (head/API worker): claim issuance on
   `POST /api/session-commands/:id/claim`, ack acceptance on `.../ack` with the
   token, generation source of truth (hub counter echoed + native
   `created_at_ms` bound host-side), capture truncation markers. Host function
   `handle_claimed_command` is ready; installing it is API worker + head scope.
2. **Physical qualification** (head-owned, later): real Grok Bot box → Hetzner
   throwaway loop, laptop/Win35 recorded separately. The 28 focused tests are
   source evidence only (fixture `a`, never live sessions).
3. **Native envelope versioning**: ACK proof depends on `reply_to`/`from`/`data`
   fields observed read-only on 2026-10-17; if the native schema versions,
   re-verify `verify_ack_via_reply` mapping.
4. **cwd/tier**: Fleet DTO carries no `cwd`; server must reject unsupported
   cwd/tier before effects until the qualified integration is extended
   (`resolve_cwd` validates intent paths only). Exact seam: Fleet offer dict ↔
   `Dispatcher.dispatch` in `host/agent_hub_host/fleet.py`.
5. **Independent review**: different-model review of the exact version before
   integration; head integrates.
