# Independent review — authenticated host machine binding (`8ff51a1`)

**SCOPED ACCEPT — exact pin `8ff51a1fac456849529d2e8728eae454caf9ec75`.**

Reviewer: `platform-host-binding-review` / `grok-4.6` (session `1a465164-e424-4368-9c1d-d425fa48f47c`). Author of this SHA: `platform-host-binding` / `gpt-6.1-sol` (distinct identity and model). Head retains Agent Branches task0425 fork/WIP, integration and recovery.

Review: approved sha=8ff51a1fac456849529d2e8728eae454caf9ec75 reviewer=platform-host-binding-review/grok-4.6

Source acceptance of this SHA does not integrate to main, does not enroll a host, and does not close live runtime, physical cross-host adoption, or M11. Full roadmap M1–M12+M2b remains active.

## Verdict

F-POST-1 is repaired on this exact source. A per-operation `_TokenExchange` receipt replaces the old ambient `_pending_machine_binding`. Device login and refresh accept an explicit valid `machine_id` string or explicit `null` only from the same successful `POST /api/device/token` or `POST /api/token/refresh` at the configured origin. Absence of the key preserves the prior local binding. Malformed, revoked, failed, foreign, cached, or one-shot-consumed receipts leave the old private config. Public token metadata is validated and is not used as a second grant. Atomic `private_fs` save and the conversation-bridge machine-mismatch HOLD remain intact.

F-POST-2 runbook procedure matches the current local hub server API for the operations it names: no admin rebind endpoint, `409 agent_machine_conflict` on a non-null change, `DELETE` of the exact qualified actor then `POST /api/agents`, CSRF on session mutations, credential revocation on delete, and actor-name reuse is not continuity. One Low documentation wording defect is recorded below; it does not reverse the source verdict. Head may publish a narrow runbook wording correction after this pin.

Same-user Python object receipts are local correlation only. Server authentication remains the actual gate. This review invents no sandbox.

## Exact objects

| Object | Full SHA | Role |
| --- | --- | --- |
| Reviewed pin / report parent | `8ff51a1fac456849529d2e8728eae454caf9ec75` | Runbook commit; immediate parent of this report |
| Source commit | `0139e89914a8ab072896422d15fd1c7c69399204` | Binding repair; `HEAD^` of `8ff51a1` |
| Reviewed range base | `6cf0c185495a33cf04ec2b986c3c901ed3de2cda` | Hub main at candidate construction; `HEAD^^` |

Mechanically verified in this worktree: `git rev-parse HEAD` = `8ff51a1…`, `HEAD^` = `0139e89…`, `HEAD^^` = `6cf0c18…`. Owned delta `6cf0c18..8ff51a1` is six paths. `git diff --check` is clean.

### Owned delta SHA-256 at `8ff51a1`

| Path | SHA-256 | Bytes |
| --- | --- | --- |
| `docs/HOST-MACHINE-BINDING.md` | `fbfcd1cd2de37263ca13c16ab67c3b2a32d88d54eca47a9207b895075e914061` | 6991 |
| `host/agent_hub_host/api.py` | `e827b65206b5f849c002985ba6895e5bbfc6b62460f5a97ffc5220d8ec6f170d` | 20142 |
| `host/agent_hub_host/auth.py` | `4306d04e07fac4e1fa775c8d5afed2e0df236175407936fb2e4bbb4aae74df0f` | 6028 |
| `host/agent_hub_host/config.py` | `55daf41a8a6305a5fb376c25c5f20f7c7ca18ed1ec645743749fa9bc39075126` | 8436 |
| `host/tests/test_auth.py` | `a553009a0bbb296b63e03e0bef17ce32ffd219b2d659264c611409d9d08915fb` | 5382 |
| `host/tests/test_machine_binding.py` | `d8100865b25e3d41c1c4a20b79e4ce05afae2efeb2030ebbe9d895487896cbba` | 16620 |

`private_fs.py`, `win32_fs.py`, server TypeScript, CLI, and old reports are outside the delta.

## F-POST-1 — token receipt (PASS)

**Location.** `host/agent_hub_host/config.py` `_TokenExchange`; `api.safe_http_json(..., _token_exchange=)`; `auth.device_login` / `auth.refresh_token`.

Independent source read plus author 30 tests and 7 novel probes:

- Explicit string replaces an older ID; explicit `null` removes the key; absent key preserves the legacy binding on both device and refresh.
- Receipt is bound to that operation, exact POST URL, exact response object identity, starting config, and one-shot `close()` even when `atomic_write` fails.
- Foreign origin is rejected before HTTP. Query, params, GET, and the other token path cannot record. A copied dict, mutated `access_token`, cached `safe_http_json` return, or later `update_config(options=...)` cannot consume a spent receipt.
- Concurrent stale save: an earlier receipt cannot overwrite a newer committed config (`Config changed`).
- Revoked refresh (400 `invalid_grant` / 401) no longer `clear_config()`; failed re-pair leaves the old file.
- Refresh missing `refresh_token` keeps the old refresh; explicit `null` removes it. Device login without a refresh key drops the previous refresh (new pairing).
- Public metadata (`token_type`, `expires_in`, `scope`) is type-checked. Invalid metadata rejects the whole response. Valid metadata is not written into config and does not swap a different access token. Extra keys (`secret`, `actor`, `admin`) are not persisted. Exception text does not echo fixture `rejected` values.
- True-positive empty-config login saves the new binding with `0600`/`0700`.
- Cross-origin and cross-operation commits fail closed (`response mismatch`).

`_persist_registration_machine_id` on `POST /api/machines/register` remains a distinct host-enrollment path that may set an unbound `machine_id` via `update_config`. It is pre-existing, origin-checked, and still conflicts with an existing ID. It is not ambient pending authority from general HTTP.

Conversation bridge `test_credential_and_registered_machine_mismatch` still HOLDs. `save_config` still delegates to `private_fs.atomic_write`. Windows portable HOLD source is unchanged.

## F-POST-2 — migration runbook vs current local server API (PASS, Low wording)

Checked against local `src/hub.ts`, `src/agent_registry.ts`, `src/schema.ts` at this pin. No registry mutation, login, enrollment, or deploy was performed.

| Runbook claim | Local source |
| --- | --- |
| No admin rebind | `handleAgentRegistry` is GET/POST/DELETE only |
| Existing non-null change | `409 agent_machine_conflict` (`hub.ts` approve transaction) |
| Existing POST | `409 agent_exists` |
| Bare multi-project tag | `409 ambiguous_agent_tag` |
| Move path | `DELETE /api/agents/<URL-encoded-project:tag>` then `POST /api/agents` with `tag`/`project`/`role`/`machine_id` |
| CSRF | Session non-GET in `authenticateBusRequest` requires `x-csrf-token`; device approve also `verifyCsrf` |
| Credential death | `agents_revoke_credentials` AFTER DELETE; pairings denied |
| Actor reuse | Not automatic continuity; runbook states restoring the old file cannot revive revoked credentials |
| Participants check | `GET /api/conversations/participants` returns `self.actor`; registration machine is on participant/`native_binding` |
| Refresh copies binding | Refresh insert copies `cred.machine_id`; JSON always includes `machine_id` (string or `null`) |

POST body whitelist is exactly `tag`, `project`, `role`, `machine_id` (`hub.ts:1003`). Extra keys are `400 invalid_agent`.

### LOW (non-blocking, documentation): leftover physical columns vs “no … columns”

**Severity.** Low. Stylistic/API-doc precision. Not a source binding defect.

**Location.** `docs/HOST-MACHINE-BINDING.md` line 84: “The agents table has no registration `workspace` or `session_id` columns.”

**Actual local schema.** `src/schema.ts` `SCHEMA_SQL` `CREATE TABLE IF NOT EXISTS agents` (`schema.ts:163-185`) and `Hub.initDb` `ensureColumns('agents', { … session_id, workspace … })` (`hub.ts:216-237`) still declare leftover `workspace` and `session_id` columns. `conversations.ts` may copy those leftover fields into `native_binding`. They are not written by `POST /api/agents` and are not current binding authority.

**Correct statement.** The registration API does not accept `workspace` or `session_id`. Leftover table columns, if present, are not continuity after DELETE+POST+re-pair.

**Repro.** Read those line ranges at `8ff51a1`. No live Durable Object was queried.

**Fix.** Head-owned narrow wording on the runbook after this verdict; do not re-pin source hashes or repeat the 30 tests for that sentence. Original `8ff51a1` remains the reviewed pin.

This is separated from F-POST-1 source behavior.

## Process incident (preserved, no waiver)

Early source commit `0139e89` used `core.hooksPath=/dev/null`. Author disclosed it; head prohibited a repeat. Final `8ff51a1` used a normal `git commit`. This worktree currently has no `core.hooksPath` override; resolved hooks directory contains only sample hooks. That observation is not a retroactive waiver. Independent review and normal head integration remain required regardless of harmless sample hooks.

## Independent execution (this reviewer)

Serial `taskset -c 0`, stdlib only, synthetic HOME/XDG fixtures, `PYTHONDONTWRITEBYTECODE=1`, own `TMPDIR` / `PYTHONPYCACHEPREFIX` under this worktree `.local/tmp`, umask 077. No Node, no installs, no source writes, no assertion weakening.

| Run | Evidence | Result |
| --- | --- | --- |
| Focused 30 | `.local/review/20261017T084436-461dbe/` | `Ran 30 tests` `OK` exit 0; 17.588s |
| Mock HTTP CLI | `.local/review/20261017T084459-1c03cc/` | `TestHttpCliMock.test_cli_full_flow` 1/1 OK exit 0 |
| Independent loader miss | `.local/review/20261017T084512-34d55f/` | **preserved failure**: unittest name-loaded the worktree path; runner defect, not source |
| Independent 7 | `.local/review/20261017T084532-bdacb3/` | 7/7 OK exit 0 after direct-script invoke |

Focused 30 = `test_config` (7) + `test_auth` (4) + `test_machine_binding` (18) + bridge mismatch (1). Existing ResourceWarning from a legacy `HTTPError` mock appeared in the 30-test log and did not fail.

Independent probes: empty-config true-positive; cross-origin/operation receipts; copied payload identity; extra keys / admin-looking scope not persisted; malformed `expires_in=False` consumes the receipt; non-token paths and query rejected; failed save consumes refresh receipt and keeps `0600`.

### Resources (this reviewer)

Combined `.local/review` + `.local/tmp` sampled scratch peak **106496** allocated / **75326** apparent bytes (24 entries). Max child RSS **40740** KiB. Sampled cgroup memory **116654080** bytes, tasks **59**. Limits `1572864000` bytes / `256` tasks. Early 4 MiB flag false. Hard 6 MiB not approached. Author `.local/binding` evidence was read-only.

## Limits

- Source and synthetic HTTP only. Not physical adoption, live Worker, enrollment, or M11.
- No Agent Branches mutation, no push, no deploy, no DNS, no credential or registry writes.
- Windows NTFS/Win32 behavior is source-unchanged, not re-proven on Windows.
- Same-user local config remains writable by the same uid; receipts do not sandbox that.

## Head next

Normal fast-forward of accepted `8ff51a1` onto hub main after this report commit. Optional narrow runbook wording for leftover agents columns. Do not treat this review as runtime or multi-host evidence.
