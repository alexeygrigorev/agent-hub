# Independent Review: M12e Connected Hub Session Control (`SESSION-CONTROL`)

- **Verdict**: **`SCOPED REVISE`** on exact commit `f9417de39d7818f18cf6f4a3a8770c6a67518db0`
- **Reviewer**: `platform-session-compose-review` (native session `0aaf19de-eddd-40a7-928e-edad260eb809`, model: **Gemini 4 Argon High**)
- **Author / Implementation Under Review**: OpenCode **Muse Spark 1.3** (native session `ae075e9a`, Agent Branches `task0430`, different model family)
- **Worktree**: `/home/alexey/git/agent-hub-wt-platform-session-compose-review`
- **Review Date**: 2026-10-17

---

## 1. Executive Summary & Scope Verdict

Commit `f9417de39d7818f18cf6f4a3a8770c6a67518db0` (`[skip ci] M12e compose: real-Worker composed loop proves server agreement`) composes the M12e Hub session-control Worker/Durable Object queue (`src/session_control.ts`, `src/hub.ts`) with the Python public CLI (`host/agent_hub_host/session_control.py`, `host/agent_hub_host/cli.py`), bounded host receiver (`host/agent_hub_host/session_receiver.py`), local journaled executor (`host/agent_hub_host/session_executor.py`), and native `aplexer` CLI adapter (`host/agent_hub_host/native_sessions.py`).

While the composition resolves several earlier DTO mismatches between the API and host branches (such as `ack.native.native_id` carrying the bound target session UUID while envelope IDs travel in `proof.native_message_id` / `proof.reply_to_native_id`, dual ISO/integer claim expiry fields, `recv-<id>.json` attempt journaling before `/claim`, and pre-effect `409 unsupported_session_option` holds for `tier` and non-root `cwd`), **independent code inspection and novel adversarial testing against the actual Worker/SQLite and host receiver/executor uncovered four HIGH-severity and two MEDIUM-severity defects that require `SCOPED REVISE`**:

1. **[F1 — HIGH] Synchronous 0ms Reply Check and Immediate `outcome: "uncertain"` ACK Lock Out Real Asynchronous Recipient Replies**:
   In `host/agent_hub_host/session_executor.py:660-676` (`_run_claimed_effect`), `verify_ack_via_reply` is invoked synchronously in the same turn immediately after `send_prompt` (`a message send`). Unless the recipient session has already written a correlated `a message reply` within 0 milliseconds *during* the `a message send` subprocess itself (which only happens in the author's synthetic `REAL_SESSION_FAKE_A` auto-reply fixture), `verify_ack_via_reply` returns `state="unknown"`, causing `handle_claimed_command` to journal `state="unknown"` in `cmd-<id>.json` and immediately POST `outcome="uncertain"` to `/api/session-commands/:id/ack`. On the server (`src/session_control.ts:600, 654-657`), `outcome="uncertain"` permanently transitions the command row to `state='uncertain'`, which is never returned by `/offers` again and rejects any subsequent reconciled `/ack` with `409 command_uncertain`. Locally, `handle_claimed_command` (`session_executor.py:554-563`) treats `state="unknown"` as already completed (`not in (None, "uncertain")`) and returns `replayed=True` without ever re-checking `verify_ack_via_reply`.
2. **[F2 — HIGH] Unbound Host Receiver Emits Anonymous Native Envelopes (`from: {"external": true}`) That Target Sessions Cannot Reply To**:
   As documented and enforced by `host/agent_hub_host/native_bridge.py` (`qualify_native_service_binding`, `create_fake_aplexer_script`, modeling Rust `aplexer` `message_routing.rs:218-248`), running `a message send` from an unbound host process (without a qualified caller session in the target workspace) emits `"from": {"external": true}` with no sender tag, and any recipient attempting `a message reply <id>` fails with `Error: original message <id> was sent anonymously (no sender tag)`. Neither `native_sessions.py:send_inbox`, `session_executor.py`, nor `session_receiver.py` calls `qualify_native_service_binding` or checks `envelope.get("from")`. All three author host test suites (`test_session_real_worker.py:123`, `test_session_control_loop.py:108`, `test_session_executor.py:167`) masked this gap by hardcoding a fabricated `"from": {"session_id": "00000000-0000-4000-8000-000000000099", "tag": "test-caller"}` inside their mock `a` scripts.
3. **[F3 — HIGH] Missing Host Family & Machine Authorization on `GET /api/session-commands/offers`, Pre-Bind Family Hijack, Deleted-Machine Persistence, and `ownerOk` Token Short-Circuit**:
   - `GET /api/session-commands/offers` (`src/session_control.ts:476-530`) never calls `this.hostOk(session, caller)` or `this.presentingFamily(caller)`. A re-enrolled or foreign host credential family for the same `machine_id` can poll `/offers`, read private command payloads (including operator prompt bodies) for sessions bound to a prior `host_family`, and mutate their SQLite state from `recorded` to `transported`.
   - `hostOk` (`src/session_control.ts:206-213`) never queries the `machines` table (`SELECT id, credential_id FROM machines WHERE id = ?`). As a result: (a) before `/binding` sets `session.host_family`, a superseded old host credential family can call `POST /api/sessions/:id/binding` first and lock `session.host_family` to the old family, locking out the active enrolled machine credential (`409 host_credential_changed`); and (b) deleting a machine row from `machines` leaves its `kind='host'` credential accepted by `hostOk` and `/offers`.
   - `ownerOk` (`src/session_control.ts:194`) short-circuits on `if (row.owner_token_id && caller.tokenId && row.owner_token_id === caller.tokenId) return true;` before `this.presentingFamily(caller)` and without re-verifying live `credentials`/`agents` state inside the synchronous transaction after `await body(request)`.
4. **[F4 — HIGH] Lost `/ack` HTTP Response Permanently Strands Commands Because `cmd-<id>.json` Records Final State Before `/ack` Succeeds**:
   In `host/agent_hub_host/session_executor.py:586-630`, `handle_claimed_command` writes the final outcome state (`"captured"`, `"stopped"`, `"acknowledged"`, or `"unknown"`) to `cmd-<id>.json` at line 591 *before* POSTing `/api/session-commands/:id/ack` at line 624, and swallows HTTP errors into `ack_result = {"submitted": False, ...}`. On any subsequent `receive_once` retry with the replayed claim token, lines 554-563 see `prior.get("state") not in (None, "uncertain")` and immediately return `{**prior, "replayed": True}` **without re-submitting `POST /ack`**, stranding the server command in `claimed` until lease expiry marks it `uncertain`.
5. **[F5 — MEDIUM] `POST /api/sessions/:id/binding` Does Not Reconcile Against Canonical `fleet_launches`**:
   `applyBinding` (`src/session_control.ts:251-281`) checks only that `session_control_sessions.fleet_launch_id` is non-null. It never queries or updates `fleet_launches` (`SELECT state, machine_id, workspace FROM fleet_launches WHERE id = ?`), allowing a host to bind an unclaimed (`offered`) or `failed` Fleet launch to an arbitrary `tag` and `workspace` while `fleet_launches.state` remains `'offered'` or `'failed'`.
6. **[F6 — MEDIUM] Unpaginated Client `wait_for_command` / `hub sessions list --state`, Plus `verify_ack_via_reply` Dead Body-Fallback and Unchecked `sender.created_at_ms`**:
   - `wait_for_command` (`host/agent_hub_host/session_control.py:108-121`) fetches only `GET /api/sessions/<id>/commands?limit=100` (ascending by `created_at, id`) without following `next_cursor`. Once a session has 100 historical terminal commands, command #101 is on page 2 and `wait_for_command` immediately fails with `ValueError: Command missing from server listing; reconcile before retrying`.
   - `execute` (`session_control.py:173-181`) for `hub sessions list --state <state> --limit <N>` filters client-side on the first page (`?limit=N`) without following `next_cursor`.
   - `verify_ack_via_reply` (`host/agent_hub_host/native_sessions.py:509-542`) checks `if echoed_corr != correlation["correlation_id"]: continue` on `env.get("data")` unconditionally before line 513 (`if not expected_digest and correlation["correlation_id"] not in body_text: continue`), rendering line 513 dead code; and it never rejects a reply whose `sender.get("created_at_ms")` mismatches `target.created_at_ms` (while `session_executor.py:614` overwrites `sender_created_at_ms` in `ack_payload["proof"]` with `target.created_at_ms`).

---

## 2. Frozen Manifest & Git Commit Chain Verification

### 2.1 Frozen Source Manifest & Proof
Mechanically verified against `HEAD` (`f9417de39d7818f18cf6f4a3a8770c6a67518db0`) and the working tree (recorded in `.local/review/manifest-verification.json`):
- **Manifest file**: `/home/alexey/git/agent-coordination/.local/platform/session-connected-source-manifest.json`
  - Size: `27,873` bytes
  - Raw file SHA-256: `27ea778a9afedf0ba3584db1572ead82a701fa7237db891bb08470e270d1de82`
  - Compact sorted JSON SHA-256 (`json.dumps(data, sort_keys=True, separators=(',', ':'))`): `11f084b9fe73c9d031ea18cc4bc0a13e2da58246da28a4d80b55dee943d51483`
- **Proof file**: `/home/alexey/git/agent-coordination/.local/platform/session-connected-source-proof.json`
  - Records `manifest_entries: 243` and matching prefix `11f084b9fe73c9d0` (`manifest_sha256: 11f084b9fe73c9d031ea18cc4bc0a13e2da58246da28a4d80b55dee943d51483`).
- **Pair verification**:
  - All **243** `(path, sha256)` pairs in `files` match both `HEAD` blobs and the working tree with **0 mismatches**, **0 missing files**, and **0 corrupt entries**.
  - Total tracked files in `git ls-files`: `264` (`243` in manifest + `21` pre-existing `docs/proposals/*` [6] and `docs/reviews/*` [15] unchanged since `6bc91a1278250b374ea1d0b11acd353fae51a8cb`).
  - All **17** paths modified in `6bc91a1278250b374ea1d0b11acd353fae51a8cb..f9417de39d7818f18cf6f4a3a8770c6a67518db0` are present and hash-verified in the 243-entry manifest.

### 2.2 Verified Git Commit Graph (`6bc91a1..f9417de`)

| Commit SHA | Parents | Author / Trailer | Exact Subject |
| :--- | :--- | :--- | :--- |
| `6bc91a1278250b374ea1d0b11acd353fae51a8cb` | `4c7cf04bd689b81c867bd2622f2c116bede9d94a` | Alexey (`platform`) | `docs: correct independent review Git metadata before publication [skip ci]` |
| `b66796cda663a2032731a12bc8e5a9bfccd1de12` | `6bc91a1278250b374ea1d0b11acd353fae51a8cb` | `platform-session-api` / Muse Spark 1.3 | `[skip ci] M12e session control: durable hub sessions, commands, fleet start hook, CLI` |
| `2fefbb406b05b0dd3f6d6e81225be4c1d61560e4` | `b66796cda663a2032731a12bc8e5a9bfccd1de12` | `platform-session-api-r2` / Muse Spark 1.3 | `[skip ci] M12e session control R2: claim tokens, reply proof, family continuity, --wait` |
| `cd0e9d099efea884ee9e52fcbe7c0901e03d4128` | `2fefbb406b05b0dd3f6d6e81225be4c1d61560e4` | `platform-session-api-r2` / Muse Spark 1.3 | `[skip ci] M12e session control: head amendments (cwd/proof/attempt)` |
| `36534db6a66e71fda52bde4a832c0b264e2fabfc41` | `6bc91a1278250b374ea1d0b11acd353fae51a8cb` | `platform-session-host` / Muse Spark 1.3 | `feat(host): M12e native session adapter and Fleet-routed session executor [skip ci]` |
| `69ae9f1e9a8f4cf6e33fc205080f7ee1350ca15e` | `36534db6a66e71fda52bde4a832c0b264e2fabfc41` | `platform-session-host-r2` / Muse Spark 1.3 | `fix(host): M12e R2 critical repairs — retire enter-ACK, reply proof, claim receiver, locks [skip ci]` |
| `d841c6456badd27c1026d74abb3cae7e26473cf5` | `6bc91a1278250b374ea1d0b11acd353fae51a8cb`, `cd0e9d099efea884ee9e52fcbe7c0901e03d4128` | Alexey | `chore: compose hub session API with accepted main [skip ci]` |
| `d84ca875898a3aaf89ef8dabdefcf93c7f4640b6` | `d841c6456badd27c1026d74abb3cae7e26473cf5`, `69ae9f1e9a8f4cf6e33fc205080f7ee1350ca15e` | Alexey | `chore: compose native session host adapter [skip ci]` |
| `9bb9a7777772d778c011b4c860a0947a663b2d3e` | `d84ca875898a3aaf89ef8dabdefcf93c7f4640b6` | `platform-session-compose` / Muse Spark 1.3 (`ae075e9a`) | `[skip ci] M12e compose: server claim/offers/binding contract for connected receiver` |
| `15faace087551bd551621575dc77ea21dac1f737` | `9bb9a7777772d778c011b4c860a0947a663b2d3e` | `platform-session-compose` / Muse Spark 1.3 (`ae075e9a`) | `[skip ci] M12e compose: corrected host proof + bounded host-receive receiver` |
| `0aabc8c05f43374b17aea3d7e7775fb8f22808a3` | `15faace087551bd551621575dc77ea21dac1f737` | `platform-session-compose` / Muse Spark 1.3 (`ae075e9a`) | `[skip ci] M12e compose: connected loop tests + corrected contract docs` |
| `f9417de39d7818f18cf6f4a3a8770c6a67518db0` | `0aabc8c05f43374b17aea3d7e7775fb8f22808a3` | `platform-session-compose` / Muse Spark 1.3 (`ae075e9a`) | `[skip ci] M12e compose: real-Worker composed loop proves server agreement` |

---

## 3. Detailed Findings

### Summary Table

| ID | Severity | Component / Location | Summary |
| :--- | :--- | :--- | :--- |
| **F1** | **HIGH** | [session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L552-L676), [session_receiver.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_receiver.py#L153), [session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L598-L657) | Synchronous 0ms reply check after `send_prompt` immediately acks `outcome="uncertain"` when recipient replies asynchronously; server permanently locks command in `state='uncertain'` (`409 command_uncertain`) and local journal `state="unknown"` prevents read-only reconciliation. |
| **F2** | **HIGH** | [native_sessions.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_sessions.py#L340-L388), [session_receiver.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_receiver.py#L195-L306), [native_bridge.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_bridge.py#L75-L210) | Unbound host service process running `a message send` emits `"from": {"external": true}` (no sender tag); target sessions cannot run `a message reply` (`was sent anonymously`), yet `send_inbox` / `receive_once` never qualify sender binding or report the capability gap. |
| **F3** | **HIGH** | [session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L191-L213), [session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L476-L530), [hub.ts](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/hub.ts#L419-L422) | `GET /api/session-commands/offers` skips `hostOk` / `presentingFamily`, leaking private prompt bodies to foreign/re-enrolled host credential families and mutating `recorded -> transported`; `hostOk` never checks `machines` (`credential_id` or existence); `ownerOk` short-circuits on `owner_token_id === caller.tokenId` before `presentingFamily`. |
| **F4** | **HIGH** | [session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L552-L630) | `handle_claimed_command` writes terminal state to `cmd-<id>.json` before `POST /ack`; if the `/ack` HTTP response is lost, subsequent `receive_once` cycles return `replayed=True` without re-submitting `POST /ack`, stranding the command on the server. |
| **F5** | **MEDIUM** | [session_control.ts](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L251-L281) | `applyBinding` (`POST /api/sessions/:id/binding`) never queries or updates canonical `fleet_launches`, allowing binding of `offered`/`failed` launches and arbitrary tags/workspaces. |
| **F6** | **MEDIUM** | [session_control.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_control.py#L108-L181), [native_sessions.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_sessions.py#L505-L542), [session_executor.py](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L614) | `wait_for_command` (`?limit=100`) and `hub sessions list --state` ignore `next_cursor` pagination; `verify_ack_via_reply` has unreachable body-correlation fallback and does not reject mismatched `sender.created_at_ms`. |

---

### Finding F1 (HIGH): Synchronous 0ms Reply Check and Immediate `outcome: "uncertain"` ACK Lock Out Real Asynchronous Recipient Replies

- **Locations**:
  - [`offer_to_command`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_receiver.py#L148-L154) (`host/agent_hub_host/session_receiver.py:153`)
  - [`handle_claimed_command`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L550-L630) and [`_run_claimed_effect`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L650-L676) (`host/agent_hub_host/session_executor.py:552-563, 586-604, 660-676`)
  - [`ack`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L583-L664) (`src/session_control.ts:600, 654-657`)
- **Problem**:
  1. `offer_to_command` sets `command["await_ack"] = True` on every `send` offer.
  2. In `_run_claimed_effect` (`session_executor.py:653-676`), immediately after `send_prompt` runs `a message send` (which only persists the message into the target's inbox), line 661 synchronously calls `verify_ack_via_reply` with zero wait or poll window.
  3. For any real agent session (which reads its inbox and replies asynchronously, rather than executing a reply inside the `a message send` subprocess itself), `verify_ack_via_reply` returns `AckResult(state="unknown")`. `_run_claimed_effect` then returns `{"state": "unknown", "ack_outcome": "uncertain", ...}`.
  4. `handle_claimed_command` writes `state: "unknown"` to `cmd-<id>.json` (line 591) and immediately POSTs `{"outcome": "uncertain", ...}` to `/api/session-commands/<id>/ack` (line 624).
  5. On the Hub server (`src/session_control.ts:654-657`), receiving `outcome: "uncertain"` permanently updates `session_control_commands.state = 'uncertain'`. Once `state = 'uncertain'`:
     - `GET /api/session-commands/offers` (`src/session_control.ts:479`) never returns the command again (`state IN ('recorded', 'transported')`).
     - Any subsequent `POST /api/session-commands/:id/ack` carrying the delayed reply proof is rejected at `src/session_control.ts:600`:
       ```ts
       if (cmd.state === 'refused' || cmd.state === 'uncertain') return err(409, 'command_' + cmd.state);
       ```
  6. Even locally on the host, `handle_claimed_command` (`session_executor.py:554-563`) checks `if prior.get("attempted") and prior.get("state") in (None, "uncertain"):`. Because `_run_claimed_effect` set `prior["state"] = "unknown"` (not `"uncertain"`), lines 559 and 563 treat `cmd-<id>.json` as a finished command and return `{**prior, "replayed": True}` without ever calling `verify_ack_via_reply` again.
- **Executed Repro**:
  - Proven in `test_probe_1b_delayed_recipient_reply_after_first_receive_once_locked_out` (`.local/review/test_novel_host_probes.py`, run `10e-pytest-novel-host-probes`) and `probe_w6` (`.local/review/novel-worker-probes.test.ts`, run `11g-vitest-novel-worker-probes`).
  - When the recipient reply arrives in the native message log *after* the first `receive_once` cycle, direct read-only `verify_ack_via_reply` confirms `state == "acknowledged"`, but `receive_once` replays `state == "unknown"` with `ack_submitted == False`, and the Worker rejects any post-reconciliation `/ack` with `409 {"error": "command_uncertain"}`.
- **Required Fix**:
  - While a `send` claim lease is still active and `original_native_message_id` has been recorded in `cmd-<id>.json` without a verified reply yet, do **not** immediately finalize the server command into terminal `uncertain` on the 0ms check; or allow a claimed command in `claimed` / `uncertain` (with the same active `claim_token` and `original_native_message_id`) to be reconciled by a read-only `verify_ack_via_reply` pass in `receive_once` / `handle_claimed_command` without performing a second `a message send`.
  - In `handle_claimed_command`, when `prior.get("state") == "unknown"` and `prior.get("original_native_message_id")` is present, re-run read-only `verify_ack_via_reply` against the journaled `original_native_message_id` and submit the `delivered` proof to `/ack` once observed.

---

### Finding F2 (HIGH): Unbound Host Receiver Emits Anonymous Native Envelopes (`from: {"external": true}`) That Target Sessions Cannot Reply To

- **Locations**:
  - [`send_inbox`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_sessions.py#L340-L388) (`host/agent_hub_host/native_sessions.py:340-388`)
  - [`receive_once`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_receiver.py#L195-L306) (`host/agent_hub_host/session_receiver.py:195-306`)
  - Contrast with [`qualify_native_service_binding`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_bridge.py#L75-L210) and [`create_fake_aplexer_script`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_bridge.py#L522-L593) (`host/agent_hub_host/native_bridge.py`)
- **Problem**:
  - In the installed Rust `aplexer` CLI (`message_routing.rs:218-248`, modeled faithfully in `native_bridge.py:522-534, 587-593`), `a message send` resolves the sender from the calling process's bound `aplexer` session (`APLEXER_SESSION_ID` in the same canonical workspace). When called from an ordinary unbound host process (`hub sessions host-receive --once` run outside a workspace-bound service session), `a message send` succeeds with exit code `0` and emits `"from": {"external": true}` with no `tag`.
  - When the recipient session later attempts `a message reply <envelope_id> "..."`, `aplexer` rejects the reply with exit code `1`:
    `Error: original message <id> was sent anonymously (no sender tag)`.
  - Unlike `MailboxBridge` in `host/agent_hub_host/bridge.py` (which calls `native_bridge.qualify_native_service_binding` before forwarding and raises `BridgeCapabilityGap` if unbound), `native_sessions.py:send_inbox`, `session_executor.py`, and `session_receiver.py` never qualify the native sender binding and never inspect `envelope.get("from")` on the output of `a message send --json`.
  - All three author test fixtures ([`test_session_real_worker.py:123`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/tests/test_session_real_worker.py#L123), [`test_session_control_loop.py:108`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/tests/test_session_control_loop.py#L108), [`test_session_executor.py:167`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/tests/test_session_executor.py#L167)) masked this by hardcoding `"from": {"session_id": "00000000-0000-4000-8000-000000000099", "tag": "test-caller", "workspace": ws}` inside their custom fake `a` scripts.
- **Executed Repro**:
  - Proven in `test_probe_1a_unbound_native_sender_emits_anonymous_and_reply_fails` (`.local/review/test_novel_host_probes.py`, run `10e-pytest-novel-host-probes`) using the repository's own authoritative `native_bridge.create_fake_aplexer_script`:
    - `qualify_native_service_binding` returns `None` (unbound).
    - `native_sessions.send_inbox` still succeeds and returns `DeliveryResult(state="recorded")`, while the persisted mailbox envelope has `"from": {"external": true}`.
    - The target session's `a message reply <id>` fails with exit code `1` (`Error: original message ... was sent anonymously (no sender tag)`).
- **Required Fix**:
  - Before executing a `send` command (which requires a correlated recipient reply to reach `acked`), `session_receiver` / `session_executor` / `send_inbox` must qualify that the host process holds a genuine workspace-bound native sender identity (via `native_bridge.qualify_native_service_binding` or verifying `whoami --json` / `envelope["from"]["tag"]` without `--from` overrides).
  - If the host process is unbound in the target workspace (or if `envelope.get("from", {}).get("external") is True` / `not envelope.get("from", {}).get("tag")`), refuse/hold honestly with an explicit capability-gap reason (`native_sender_unbound`) rather than claiming a reply-capable prompt send.

---

### Finding F3 (HIGH): Missing Host Family & Machine Authorization on `GET /api/session-commands/offers`, Pre-Bind Family Hijack, Deleted-Machine Persistence, and `ownerOk` Short-Circuit

- **Locations**:
  - [`ownerOk`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L191-L203) and [`hostOk`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L206-L213) (`src/session_control.ts:191-213`)
  - [`GET /api/session-commands/offers`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L476-L530) (`src/session_control.ts:476-530`)
  - [`Hub.fetch` session-control mount](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/hub.ts#L419-L422) (`src/hub.ts:419-422`)
- **Problem**:
  1. **Unchecked `host_family` and `presentingFamily` on `/offers`**:
     In `src/session_control.ts:476-530` (`GET /api/session-commands/offers`), the handler filters only by `caller.machineId` (`WHERE s.machine_id = ?`). It **never calls `this.hostOk(session, caller)` or `this.presentingFamily(caller)`**. When a host machine is re-enrolled with a new credential family (`host-family-new`) while a session is bound to `host-family-old` (or vice versa, if a superseded unrevoked credential from another family still exists for that `machine_id`), calling `GET /api/session-commands/offers`:
     - Returns `200 OK` with the full private command `payload` (including confidential operator prompt bodies).
     - Mutates every returned command in SQLite from `state = 'recorded'` to `state = 'transported'` and sets `poll_at = now`.
  2. **Unchecked `machines` table in `hostOk` (pre-bind hijack and deleted-machine persistence)**:
     `hostOk` (`src/session_control.ts:206-213`) checks `presentingFamily(caller)` against `session.host_family`, but **never queries `machines` (`SELECT id, credential_id FROM machines WHERE id = ?`)**:
     - Before `POST /api/sessions/:id/binding` is called, `session.host_family` is `NULL`. A superseded old host credential family (not matching `machines.credential_id`'s family) can call `/binding` first, setting `session.host_family = 'host-family-superseded'` and locking out the machine's current enrolled credential (`409 host_credential_changed`).
     - When a machine is deleted from `machines`, `kind = 'host'` credentials are not revoked and `hostOk` / `/offers` continue to accept them because `machines` existence is never checked.
     - *Note on M9 refresh dependency (`probe_w5`)*: Calling `POST /api/token/refresh` (`src/hub.ts:845-850`) rotates the `credentials` row within the same `family_id` while leaving `machines.credential_id` pointing to the revoked predecessor `credential_id`. Therefore, checking `machines` in `hostOk` should join `machines.credential_id` to `credentials.family_id` (or verify that the machine exists in `machines` and that `caller`'s active `family_id` matches the `family_id` of `machines.credential_id`).
  3. **`ownerOk` short-circuits on `owner_token_id === caller.tokenId` before `presentingFamily`**:
     In `src/session_control.ts:194`:
     ```ts
     if (row.owner_token_id && caller.tokenId && row.owner_token_id === caller.tokenId) return true;
     const family = this.presentingFamily(caller);
     ```
     Because line 194 returns `true` *before* `this.presentingFamily(caller)` is called, and because `src/hub.ts:421` runs `sessionControlAuth(request)` once before `await body(request)` in `SessionControlService.handle` (unlike `IntakeService.handle` in `src/intake.ts:318-328`, which re-authenticates inside the synchronous transaction after `await body(request)`), `ownerOk` does not re-verify live SQLite credential validity (`revoked_at`, `expires_at`) or agent registry incarnation (`credentialActor`) when `row.owner_token_id === caller.tokenId`.
- **Executed Repro**:
  - Proven in `probe_w1`, `probe_w2`, `probe_w3`, and `probe_w5` (`.local/review/novel-worker-probes.test.ts`, run `11g-vitest-novel-worker-probes`).
  - *Positive check*: `probe_w3` also verified that cookie-authenticated `POST /api/sessions` with a cross-origin `Origin: https://evil.example` header is properly rejected with `403` by `verifyCsrf`.
- **Required Fix**:
  - In `GET /api/session-commands/offers`, verify `this.hostOk(session, caller).ok` before including any command in `offers` or mutating its state from `recorded` to `transported`.
  - In `hostOk`, query `SELECT m.id, c.family_id AS machine_family FROM machines m LEFT JOIN credentials c ON c.id = m.credential_id WHERE m.id = ?` to ensure the machine still exists in `machines` and, when `machine_family` is present, that `presentingFamily(caller) === machine_family`.
  - In `ownerOk`, always verify `const family = this.presentingFamily(caller); if (!family) return false;` (and for `kind === 'agent'`, verify live `credentialActor(this.sql, cred)`) inside the synchronous SQLite transaction rather than short-circuiting on `row.owner_token_id === caller.tokenId`.

---

### Finding F4 (HIGH): Lost `/ack` HTTP Response Permanently Strands Commands Because `cmd-<id>.json` Records Final State Before `/ack` Succeeds

- **Locations**:
  - [`handle_claimed_command`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L550-L630) (`host/agent_hub_host/session_executor.py:552-563, 586-630`)
  - [`receive_once`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_receiver.py#L293-L305) (`host/agent_hub_host/session_receiver.py:293-305`)
- **Problem**:
  1. In `handle_claimed_command`, after `_run_claimed_effect` completes locally (e.g., `capture` or `stop` or `send`), lines 586-591 write `record` with `"state": outcome.get("state")` (e.g. `"captured"` or `"stopped"`) to `cmd-<command_id>.json`.
  2. Lines 622-629 then attempt `request("POST", "/api/session-commands/" + command_id + "/ack", ...)`. If that HTTP request raises an exception (lost response, timeout, or transient 502), lines 627-629 catch it and return `ack: {"submitted": False, ...}` without updating `cmd-<command_id>.json`.
  3. On the next `receive_once` poll (when the command is re-discovered and the server replays the same `claim_token` for the journaled `attempt_id`), `handle_claimed_command` reads `prior` from `cmd-<command_id>.json` at lines 552-563:
     ```python
     if prior is not None:
         ...
         if prior.get("attempted") and prior.get("state") in (None, "uncertain"):
             raise SessionUncertain("Claim already attempted with unknown outcome; reconcile, no new effect")
         return {**prior, "replayed": True}
     ```
  4. Because `prior.get("state")` is `"captured"` (or `"stopped"`), line 563 immediately returns `{**prior, "replayed": True}` **without calling `request("POST", .../ack)`**! The local effect is rightly not repeated, but the `/ack` is never re-sent to the Hub, stranding the server command in `claimed` until lease expiry marks it `uncertain`.
- **Executed Repro**:
  - Proven in `test_probe_2_lost_ack_http_response_permanently_strands_command_on_replay` (`.local/review/test_novel_host_probes.py`, run `10e-pytest-novel-host-probes`): after a simulated HTTP 502 on the first `POST /ack`, the second `receive_once` cycle returns `replayed: True, ack_submitted: False` and makes `0` additional `/ack` HTTP calls.
- **Required Fix**:
  - Persist `ack_payload` and `ack_submitted: bool` inside `cmd-<command_id>.json`.
  - When `prior is not None` and `prior.get("claim_token") == proved["claim_token"]` and `prior.get("state") not in (None, "uncertain")`, if `not prior.get("ack_submitted")` and `request is not None`, re-submit `POST /api/session-commands/<id>/ack` with `{**prior["ack_payload"], "claim_token": proved["claim_token"]}` and update `ack_submitted = True` in `cmd-<command_id>.json` upon success.

---

### Finding F5 (MEDIUM): `POST /api/sessions/:id/binding` Does Not Reconcile Against Canonical `fleet_launches`

- **Location**:
  - [`applyBinding`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/src/session_control.ts#L251-L281) (`src/session_control.ts:251-281`)
- **Problem**:
  - `applyBinding` checks `if (!row.fleet_launch_id) return err(409, 'missing_fleet_launch');`, but never queries `fleet_launches` (`SELECT id, machine_id, workspace, state FROM fleet_launches WHERE id = ?`).
  - Consequently, a host can bind a session whose canonical `fleet_launches` row is still `'offered'` (never claimed/started by Fleet) or `'failed'` / `'exited'`, can supply an arbitrary `tag` (unrelated to `hub-<fleet_launch_id>`) and arbitrary `workspace`, and `fleet_launches.state` remains `'offered'` or `'failed'` while `session_control_sessions.state` transitions to `'running'`.
- **Executed Repro**:
  - Proven in `probe_w4` (`.local/review/novel-worker-probes.test.ts`, run `11g-vitest-novel-worker-probes`): after setting `fleet_launches.state = 'failed'`, `POST /api/sessions/:id/binding` with `tag: 'totally-unrelated-tag'` and `workspace: '/tmp/unrelated-foreign-workspace'` still returns `200 OK` (`state: 'running'`) while `fleet_launches.state` stays `'failed'`.
- **Required Fix**:
  - In `applyBinding`, query `SELECT id, machine_id, workspace, state, native_id FROM fleet_launches WHERE id = ?` inside the transaction. Refuse binding (`409 Conflict`) if the `fleet_launches` row is missing, belongs to a different `machine_id`, is in a terminal failure/exit state (`'failed'`, `'exited'`, `'refused'`), or if `native.tag` does not match the canonical launch tag (`'hub-' + row.fleet_launch_id`) or `fleet_launches.native_id` (when already reported).

---

### Finding F6 (MEDIUM): Unpaginated Client `wait_for_command` / `hub sessions list --state`, Plus `verify_ack_via_reply` Dead Body-Fallback and Unchecked `sender.created_at_ms`

- **Locations**:
  - [`wait_for_command`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_control.py#L108-L121) and [`execute`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_control.py#L173-L181) (`host/agent_hub_host/session_control.py:108-121, 173-181`)
  - [`verify_ack_via_reply`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/native_sessions.py#L492-L547) (`host/agent_hub_host/native_sessions.py:505-542`) and [`handle_claimed_command`](file:///home/alexey/git/agent-hub-wt-platform-session-compose-review/host/agent_hub_host/session_executor.py#L606-L615) (`host/agent_hub_host/session_executor.py:614`)
- **Problem**:
  1. **Unpaginated `wait_for_command`**:
     `GET /api/sessions/:id/commands` (`src/session_control.ts:449-473`) orders commands ascending (`ORDER BY created_at ASC, id ASC`) with a max page size of `100` and returns `next_cursor`. Note that the per-session cap of `200` (`this.maxPerSession`) counts only *pending* commands (`state IN ('recorded', 'transported', 'claimed')`), so a session can legitimately have >100 total historical commands. `wait_for_command` (`session_control.py:112`) requests only `/api/sessions/<id>/commands?limit=100` without following `next_cursor`, so command #101+ immediately raises `ValueError('Command missing from server listing; reconcile before retrying')`.
  2. **Unpaginated `hub sessions list --state`**:
     `session_control.py:174-181` fetches a single page `GET /api/sessions?limit=<N>` and filters `state == wanted` client-side without following `next_cursor`, returning fewer than `N` (or `0`) matching sessions whenever the newest `N` sessions are in other states.
  3. **Dead body-correlation fallback and unchecked `sender.created_at_ms` in `verify_ack_via_reply`**:
     - In `native_sessions.py:505-514`:
       ```python
       if isinstance(payload, dict):
           echoed_corr = str(payload.get("correlation_id", ""))
           echoed_digest = str(payload.get("body_digest", ""))
       if echoed_corr != correlation["correlation_id"]:
           continue
       if expected_digest and echoed_digest != expected_digest:
           continue
       if not expected_digest and correlation["correlation_id"] not in body_text:
           continue
       ```
       Because line 509 (`if echoed_corr != correlation["correlation_id"]: continue`) executes unconditionally even when `payload` (`env.get("data")`) is `None`, line 513 is unreachable dead code: any recipient reply sent via standard `a message reply <id> "<text>"` without `--data '{"correlation_id": ...}'` is rejected even if `body` echoes the exact correlation ID and digest.
     - In `native_sessions.py:530-541`, `sender_created = sender.get("created_at_ms")` is extracted, but never compared against `target.created_at_ms` when `sender_created` is an integer; and `session_executor.py:614` hardcodes `"sender_created_at_ms": target.created_at_ms` in `ack_payload["proof"]` instead of forwarding `evidence.get("sender_created_at_ms")`.
- **Executed Repro**:
  - Proven in `test_probe_4a_wait_for_command_fails_when_command_is_past_first_100_rows`, `test_probe_4b_execute_list_state_filter_ignores_next_cursor`, and `test_probe_5_verify_ack_via_reply_dead_body_fallback_and_unverified_sender_created_at` (`.local/review/test_novel_host_probes.py`, run `10e-pytest-novel-host-probes`).
- **Required Fix**:
  - Follow `next_cursor` (up to a bounded page count) in `wait_for_command` and `execute(list)` when `--state` is specified.
  - In `verify_ack_via_reply`, allow correlation/digest echo from either structured `data` or `body` text, verify `sender_created == target.created_at_ms` whenever `sender_created` is present, re-qualify `qualify_target(executable, target, live_only=False)` so tag reuse/incarnation drift is caught, and pass `evidence["sender_created_at_ms"]` in `session_executor.py:614`.

---

## 4. Positive Verification & Incremental vs. Full-Vision Scope

### 4.1 Verified Controls in `f9417de39d7818f18cf6f4a3a8770c6a67518db0`
- **No Second Fleet Consumer or Scheduler**: Session `start` delegates strictly through the injected `reserve` hook (`src/hub.ts:288-308` -> `fleetService.handle('/api/fleet/launches/reserve')`), preserving existing M5/M6/M8 freshness, capability, concurrency, and 15% quota reserve gates. `offer_to_command` and `handle_claimed_command` explicitly reject `start` (`held: start is Fleet-owned`).
- **Pre-Effect Rejection of Unsupported `tier` and Non-Root `cwd`**: `src/session_control.ts:397-402` rejects `tier` and non-root `cwd` (`cwd !== '.' && cwd !== './'`) with `409 unsupported_session_option` before invoking `reserve` or writing any session/command rows.
- **Retired Raw PTY Injection**: `native_sessions.py:224-227` and `session_executor.py:386-389` forbid `--enter`, `--pane`, `--no-ack`, and `--from`. `message send --to` validates that the target selector is a TAG and refuses UUID-shaped values (`native_sessions.py:354-355`).
- **Distinct IDs & Proof Replay Rejection**: `validateSendProof` (`src/session_control.ts:103-131`) enforces that `native_message_id`, `reply_to_native_id`, and `sender_native_id` are three distinct values, checks exact binding (`sender_native_id`, `sender_tag`, `sender_workspace`, `sender_generation`, `sender_created_at_ms`), and rejects proof replay across commands via unique indexes `idx_session_commands_proof_reply` and `idx_session_commands_proof_msg`.
- **Lost-GET Self-Heal & Per-Command Claim Fence**: `GET /api/session-commands/offers` self-heals `transported` rows back to `recorded` after the 60s transport TTL (`poll_at <= cutoff`) only when `claim_token IS NULL`. `get_or_create_attempt` (`session_receiver.py:69-100`) journals a stable `attempt_id` in `recv-<id>.json` under `flock` before `/claim`; same-attempt retries replay the claim token, while different-attempt claims receive `409 already_claimed`.
- **Confirmed Stop & Bounded Opt-In Capture**: `stop_session` (`native_sessions.py:612-672`) requires both terminal phase (`exited`/`dead`) and `containment_empty is True` from `a status <uuid> --json` and treats absent records as `SessionUncertain` (never exit proof). `capture_output` bounds UTF-8 bytes (`<= 65536` on server, `<= 131072` locally) and sets `privacy_unknown: True`.
- **Cookie CSRF & Narrow Operator Token**: `sessionControlAuth` (`src/hub.ts:1275-1303`) rejects mixed `Cookie` + `Authorization` headers (`403 mixed_authority`), enforces `verifyCsrf` (including `Origin` host match, verified in `probe_w3`), and prevents registered credentials from falling through to the operator token hash check.

### 4.2 Incremental Scope vs. Full M12e Vision
Even after F1–F6 are repaired, acceptance of this source candidate must **not** close full M12e/M13 or M8/M11 runtime gates:
1. `tier` and non-root `cwd` are currently held (`409 unsupported_session_option`) rather than consumed end-to-end by Fleet launch reservation and `a start`.
2. Genuine runtime qualification of a workspace-bound native service sender session on deployed hosts (so `a message send` has a non-anonymous sender tag that recipients can `a message reply` to) remains a separate runtime/deployment requirement.
3. The full M12e physical acceptance gate (real Grok Bot box -> Hetzner throwaway agent loop over enrolled credentials) remains separately pending.

---

## 5. Independent Test Execution & Continuous 100ms Resource Measurements

All commands were executed sequentially on 1 pinned CPU (`taskset -c 0`) via `.local/review/run_monitored.py` with `umask 077`, `TMPDIR=/home/alexey/git/agent-hub-wt-platform-session-compose-review/.local/tmp/runs/<run_name>`, `UV_THREADPOOL_SIZE=1`, `GOMAXPROCS=1`, `NODE_OPTIONS=--v8-pool-size=1`, and a `100ms` sampling loop measuring allocated/apparent scratch bytes across `.local`, `.wrangler`, and `dist` (`16 MiB` early guard / `20 MiB` hard cap), process-tree RSS (`1500 MiB` cap), and task count (`256` cap). All disposable test caches were retired after each run with SHA-256 and byte records appended to `.local/review/retirement-manifest.json`.

### 5.1 Author Baseline (77 Cases + `tsc`) & Compatibility Suites (64 Cases)

| Run ID | Suite / Command | Exit | Cases | Elapsed (s) | Samples (100ms) | Peak Alloc (B / MiB) | Peak Apparent (B / MiB) | Post-Retire Alloc (B) | Peak RSS (MiB) | Peak Tasks |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `01-tsc` | `./node_modules/.bin/tsc --noEmit` | `0` | typecheck | `7.107` | `10` | `65,536` (`0.06`) | `43,332` (`0.04`) | `57,344` | `265.38` | `8` |
| `02-pytest-session-control` | `pytest -v host/tests/test_session_control.py` | `0` | `12 passed` | `11.793` | `21` | `229,376` (`0.22`) | `153,774` (`0.15`) | `139,264` | `234.31` | `1` |
| `03-pytest-session-control-loop` | `pytest -v host/tests/test_session_control_loop.py` | `0` | `6 passed` | `16.040` | `26` | `286,720` (`0.27`) | `155,894` (`0.15`) | `192,512` | `235.02` | `1` |
| `04-pytest-session-executor` | `pytest -v host/tests/test_session_executor.py` | `0` | `28 passed` | `37.382` | `47` | `606,208` (`0.58`) | `358,872` (`0.34`) | `360,448` | `238.78` | `3` |
| `05-vitest-session-control` *(bad CLI flag `--poolOptions`)* | `vitest run ... --poolOptions...` | `1` | `0` *(CLI arg err)* | `1.322` | `1` | `372,736` (`0.36`) | `143,419` (`0.14`) | `143,360` | `0.00` | `1` |
| `05b-vitest-session-control` | `vitest run --config test/vitest.session-control.config.ts --configLoader runner --maxWorkers=1 --no-file-parallelism` | `0` | `26 passed` | `25.215` | `34` | `15,310,848` (`14.60`) | `15,243,303` (`14.54`) | `151,552` | `596.01` | `28` |
| `06-vitest-session-control-composed` | `vitest run --config test/vitest.session-control-composed.config.ts --configLoader runner --maxWorkers=1 --no-file-parallelism` | `0` | `4 passed` | `17.568` | `22` | `2,314,240` (`2.21`) | `2,254,935` (`2.15`) | `159,744` | `564.03` | `28` |
| `07-pytest-session-real-worker` | `pytest -v host/tests/test_session_real_worker.py` | `0` | `1 passed` | `67.113` | `90` | `3,174,400` (`3.03`) | `3,026,541` (`2.89`) | `184,320` | `571.82` | `22` |
| `08-pytest-compat` | `pytest -v host/tests/test_native_bridge_contract.py host/tests/test_intake.py` | `0` | `28 passed` (+7 subtests) | `40.389` | `80` | `1,572,864` (`1.50`) | `1,119,453` (`1.07`) | `1,208,320` | `267.72` | `3` |
| `09-vitest-intake` | `vitest run --config test/vitest.intake.config.ts --configLoader runner --maxWorkers=1 --no-file-parallelism` | `0` | `36 passed` | `20.383` | `37` | `8,028,160` (`7.66`) | `7,679,331` (`7.32`) | `1,236,992` | `593.34` | `28` |

### 5.2 Novel Adversarial Defect-Reproduction Runs (12 Expected-Vulnerable Cases Across Python & Worker/SQLite)

All intermediate fixture-setup iterations (`10`..`10d`, `11`..`11f`) are retained in `.local/review/runs/` without product inference from fixture errors. In the final probe suites `10e-pytest-novel-host-probes` (`6 passed`) and `11g-vitest-novel-worker-probes` (`6 passed`), **`passed` denotes an expected-vulnerable reproduction assertion confirming the presence of defects F1–F6 on `f9417de39d7818f18cf6f4a3a8770c6a67518db0` (plus the positive CSRF cross-origin denial sub-check in `probe_w3`), NOT repaired product success**:

| Run ID | Suite / Command | Exit | Cases / Meaning | Elapsed (s) | Samples (100ms) | Peak Alloc (B / MiB) | Peak Apparent (B / MiB) | Post-Retire Alloc (B) | Peak RSS (MiB) | Peak Tasks |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `10-pytest-novel-host-probes` *(import setup)* | `pytest -v .local/review/test_novel_host_probes.py` | `2` | `0` *(invalid fixture import)* | `21.942` | `47` | `1,327,104` (`1.27`) | `952,779` (`0.91`) | `356,352` | `245.80` | `1` |
| `10b-pytest-novel-host-probes` *(import setup)* | `pytest -v .local/review/test_novel_host_probes.py` | `2` | `0` *(invalid fixture import)* | `20.869` | `47` | `409,600` (`0.39`) | `300,060` (`0.29`) | `372,736` | `245.80` | `1` |
| `10c-pytest-novel-host-probes` *(1a helper arg)* | `pytest -v .local/review/test_novel_host_probes.py` | `1` | `5 passed, 1 failed` *(fixture arg)* | `20.637` | `48` | `430,080` (`0.41`) | `314,854` (`0.30`) | `389,120` | `240.55` | `1` |
| `10d-pytest-novel-host-probes` *(1a kwarg)* | `pytest -v .local/review/test_novel_host_probes.py` | `1` | `5 passed, 1 failed` *(fixture kwarg)* | `20.444` | `48` | `442,368` (`0.42`) | `322,637` (`0.31`) | `401,408` | `240.86` | `1` |
| **`10e-pytest-novel-host-probes`** | `pytest -v .local/review/test_novel_host_probes.py` | **`0`** | **`6 passed` *(6 expected-vulnerable repros: F1, F2, F4, F6)*** | `33.822` | `55` | `483,328` (`0.46`) | `362,241` (`0.35`) | `409,600` | `240.13` | `1` |
| `11-vitest-novel-worker-probes` *(SQL col setup)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `6 failed` *(invalid fixture SQL)* | `11.202` | `20` | `1,429,504` (`1.36`) | `1,307,223` (`1.25`) | `417,792` | `563.11` | `28` |
| `11b-vitest-novel-worker-probes` *(CSRF header)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `6 failed` *(invalid fixture CSRF)* | `15.797` | `22` | `2,031,616` (`1.94`) | `1,906,121` (`1.82`) | `434,176` | `552.30` | `28` |
| `11c-vitest-novel-worker-probes` *(start brief)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `1 passed, 5 failed` *(fixture brief)* | `15.710` | `22` | `1,937,408` (`1.85`) | `1,805,646` (`1.72`) | `450,560` | `565.23` | `28` |
| `11d-vitest-novel-worker-probes` *(fleet mode)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `1 passed, 5 failed` *(fixture mode)* | `13.911` | `21` | `1,822,720` (`1.74`) | `1,685,230` (`1.61`) | `462,848` | `549.54` | `28` |
| `11e-vitest-novel-worker-probes` *(col names)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `1 passed, 5 failed` *(fixture cols)* | `14.946` | `22` | `2,248,704` (`2.14`) | `2,106,252` (`2.01`) | `475,136` | `568.08` | `28` |
| `11f-vitest-novel-worker-probes` *(actor col)* | `vitest run --config .local/review/vitest.novel-probes.config.ts ...` | `1` | `5 passed, 1 failed` *(fixture col)* | `12.735` | `21` | `1,994,752` (`1.90`) | `1,845,565` (`1.76`) | `483,328` | `557.92` | `28` |
| **`11g-vitest-novel-worker-probes`** | `vitest run --config .local/review/vitest.novel-probes.config.ts --configLoader runner --maxWorkers=1 --no-file-parallelism` | **`0`** | **`6 passed` *(6 expected-vulnerable repros: F1, F3, F5 + M9 seam)*** | `10.944` | `21` | `2,392,064` (`2.28`) | `2,236,519` (`2.13`) | `491,520` | `554.04` | `28` |

- **Overall Peak Scratch**: `15,310,848` bytes (`14.60 MiB` allocated) / `15,243,303` bytes (`14.54 MiB` apparent) during `05b-vitest-session-control`, strictly below the `16.00 MiB` early guard and `20.00 MiB` hard cap (`guard_aborted: false` across all runs).
- **Overall Peak RSS / Tasks**: `596.01 MiB` RSS (limit `1500 MiB`), `28` tasks (limit `256`), `0` lingering child processes after every run.
