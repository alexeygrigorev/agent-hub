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
     an effect; stale `created_at_ms` refuses; expected `engine`, when given
     by the hub offer/binding, must match the live catalog row exactly — a
     real SHELL row never qualifies as a requested model engine; phase is
     liveness only, never an idle/readiness claim),
   - `send_inbox` (`message send --to TAG`; state `recorded` at most — inbox
     persistence is never transported/acknowledged). Before the effect the
     caller qualifies its genuine sender identity (`whoami --json`, no
     overrides, in ANY workspace — the installed CLI routes cross-workspace
     sends for bound senders and files replies in the author's mailbox, so
     the old same-workspace restriction is removed; an optional service pin
     may fix the authorized sender workspace). Unbound senders are refused
     with `native_sender_unbound` and no effect, because recipients could
     never reply (installed anonymous-reply rejection). After the send, the
     actual envelope `from` is validated against the qualified own binding
     by the local cross-workspace rule (`_validate_own_send_envelope`:
     envelope for the recipient mailbox, `from` the sender mailbox, both
     exact); sender fields are never fabricated to make fixtures replyable.
     The genuine sender binding is returned alongside the delivery record so
     reconciliation reads the correct mailbox without re-deriving identity.
   - `deliver_recorded` (`message deliver <envelope-id>`; explicit opt-in only;
     maps to `transported`/`refused`, never acknowledged),
   - `fetch_message_log` (bounded `message log` for an explicit mailbox,
     read-only),
   - `verify_ack_via_reply` (ACK ONLY on a reply envelope in the SENDER
     mailbox log with `reply_to` = original native envelope id, `from`
     session/tag/workspace = intended target generation, correlation id +
     body-digest echo from structured `data` OR body text, and — when the
     reply carries its sender creation value — exact match against the
     OBSERVED live catalog incarnation; else `unknown`). The target is
     re-qualified (tag reuse/incarnation drift refuses) before the log scan;
     the observed catalog row is the provenance witness: the real installed
     DTO carries NO `created_at_ms` inside envelope `from` (only
     engine/session_id/tag/workspace), so an absent value uses the observed
     row and never falls back to an expected target value. All fixtures
     match this DTO (fabricated `from.created_at_ms` removed as a
     documented fixture fix; stale-incarnation tests mutate explicitly).
   - `capture_output` (bounded tail/screen; `privacy_unknown=True` always),
   - `status_of` (read-only probe), `stop_session` (kill + CONFIRMED exit:
     UUID+tag+workspace+generation (+expected engine) checked before AND
     after, AND the owned workload proven gone via reaped cgroup, dead PIDs,
     or catalog `containment_empty` — all fields observed read-only on the
     installed `a list --json`. A vanished record or lone kill exit 0
     without containment proof is `uncertain`, never stopped),
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
    effect, runs send/capture/stop for the exact target, journals the outcome
    AND the ack payload/submission state atomically, and reports `.../ack`
    with the claim token. A send with no recipient reply yet is journaled
    `unknown` with NO server ack (never premature `uncertain`); bounded
    read-only reconciliation (`receive_once` post-cycle pass over `cmd-*`
    journals, same claim token, no second send) submits the delivered proof
    once the delayed reply is observed. A completed effect with a lost ack
    response re-submits the IDENTICAL journaled ack (same token, no new
    effect); a terminal server verdict closes the journal as `uncertain`.
    `start` rejected (Fleet-owned).

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

CORRECTED ack.native semantics (composition fix, authoritative over the
gitignored `.local/session/HOST-R2-INTERFACE.md` working copy in the original
host worktree): `ack.native.native_id` is ALWAYS the TARGET NATIVE SESSION
UUID (binding match: UUID + tag + workspace), NEVER the original message
envelope id. Envelope ids travel separately — `proof.native_message_id` is the
original delivered native envelope id, `proof.reply_to_native_id` is the reply
envelope id (its `reply_to` references the original envelope, never a hub
`cmd:` string). The server validator (`validateSendProof`) and binding match
agree on this; the old working copy describing `native_id` as the envelope id
is WRONG and must not be inferred from old messages. The shared receiver
(`session_receiver.receive_once` -> `handle_claimed_command`) implements the
corrected shape; `handle_claimed_command` also accepts the server claim expiry
as either `expires_at_ms` (int) or `expires_at` (ISO) and normalizes it.

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

## Supervised Hetzner runtime

`python -m agent_hub_host.session_host` runs Fleet reporting and session-command reception inside a genuine native Shell controller. It verifies its sender binding before effects and holds one lock per journal. Fleet report failures do not prevent capture, stop or receipt reconciliation on existing sessions.

`scripts/session-host-supervisor.py` creates and monitors only its own native controller. It compares the exact UUID, tag, workspace, engine, creation time and command before stopping that controller. No persisted caller-supplied session id is adopted. The supplied user service restarts a failed supervisor and keeps credentials in a separate private configuration:

```sh
cp deploy/agent-hub-session-host.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-hub-session-host.service
systemctl --user status agent-hub-session-host.service
```

First configure the service's private credential file, subscription/workspace mappings and journal path. The service template uses the operator's `~/git/agent-hub/.local/platform-runtime` configuration. It does not use or overwrite the default host configuration. Restart the service after changing subscription settings. Starting this service must replace a previous controller using the same journal; do not run the earlier manual polling loop alongside it.

A service restart stops its own controller, while target agent sessions remain independently bound in the Hub. Pending delivery attempts retain their journal and reconcile without repeating the native send; an unproven reply remains unknown. Runtime acceptance must include a real service restart and the public session smoke test, not merely an active systemd unit.
