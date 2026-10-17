# Session host adapter (M12e, host side)

Owner: session-host worker (actualtask0429). Scope: `host/agent_hub_host/native_sessions.py`,
`host/agent_hub_host/session_executor.py`, `host/tests/test_session_executor.py`, this doc.
Server queue HTTP, public CLI verbs and server wiring belong to the API worker; the agreed
DTO is confirmed by the head before any shared receiver is implemented
(see `.local/session/DTO-PROPOSAL.md`, gitignored working copy).

## What exists

- `native_sessions.py` — reusable native wrappers around the installed `a`/`aplexer` CLI
  (argv only, no shell, bounded output):
  - `list_sessions` (read-only, optional workspace filter, minimal projection),
  - `qualify_target` (tag→UUID+workspace binding immediately before an effect),
  - `send_inbox` (`message send --to TAG`; state `recorded` at most),
  - `send_prompt_guarded` (`send --enter --json`; `transported`/`acknowledged`/`refused`,
    `uncertain` raises),
  - `capture_output` (bounded tail or screen snapshot; never uploads),
  - `status_of` (read-only probe), `stop_session` (kill + verified exit),
  - `resolve_cwd` (workspace-root confinement, symlink-escape rejection).
- `session_executor.py` — durable intents + the existing Fleet `Dispatcher` contract:
  - `start_from_offer` delegates to `fleet.Dispatcher` (same admission gate, same
    claim/result, same `a start` argv). Adds idempotency journal + reconcile-before-retry
    (tag-based adoption of live sessions, never blind-retry). No competing launcher.
  - `send_prompt` / `capture_session_output` / `stop_controlled_session` /
    `session_status` custody-checked wrappers; `handle_session_command` pure receiver
    for later head wiring (not installed anywhere).

## State model (binding on all layers)

`recorded` (inbox persisted) ≠ `transported` (PTY bytes delivered) ≠ `acknowledged`
(working/ACK proof correlated to this request) ≠ `uncertain` (reconcile first) ≠
`refused`/`held` (no effect). Inbox persistence, PTY bytes and HTTP success are never
rendered as target-read/processed. Exit 3 (delivered but agent idle) is `transported`.

## Hard refusals (no bypass)

`--pane`, `--no-ack`, `--from`, busy/non-empty composers, UUID-shaped `--to`, tag reuse
(tag now resolves to another UUID), workspace/cwd escape, over-bound bodies/captures,
missing custody, conflicting idempotency payloads. Unqualified starts are `held`
(M6/M8 gates intact). Absent native records are never exit proof (`forget`/`prune`
remove records without stopping workloads).

## Exact remaining wiring / qualification gaps

1. **Agreed DTO** (head/API worker): endpoint paths, session table vs `fleet_sessions`
   projection, receiver-ACK transport for `acknowledged`, brief bound (proposed 16 KiB),
   stop signal/grace defaults. No server receiver until explicit agreement.
2. **Receiver wiring**: `handle_session_command` is ready to be head-wired to the agreed
   endpoints/CLI; installing it is API worker + head scope, not this worker's.
3. **Physical qualification** (head-owned, later): real Grok Bot box → Hetzner throwaway
   loop (start→send→confirm→capture→stop), laptop/Win35 recorded separately. Unit tests
   are source evidence only. The fake `a` in tests intentionally returns full scrollback
   regardless of `--bytes` (genuine CLI honors it); client truncation is defense-in-depth.
4. **Native verdict schema**: `acknowledged` requires an explicit working/ACK signal in
   the `send --enter --json` verdict; unknown future verdict fields map to `transported`
   (conservative). If the native verdict schema is versioned, re-verify the mapping.
5. **Independent review**: different-model review of the exact version before integration;
   head integrates.
