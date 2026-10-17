# Host config binding and controlled agent migration

This runbook describes source behavior and a proposed operations procedure.
It does not authorize a deployment, registry deletion, re-pairing, credential
rotation, service change or physical host migration. The platform head prepares
the evidence; root obtains Alexey's approval for the concrete operations plan.
The current operations owner must acknowledge custody before execution.

## Authenticated local config transitions

Only a successful token exchange at the configured origin can change a saved
binding. Device login uses `POST /api/device/token`; refresh uses
`POST /api/token/refresh`. The receipt belongs to that login or refresh, that
exact endpoint, and that returned access token. General HTTP responses,
request JSON, cached responses and manual config-update arguments confer no
binding authority.

| Token response | Local config after successful atomic save |
| --- | --- |
| Explicit valid `machine_id` string | Save that binding, including a legitimate change from an older ID. |
| Explicit `machine_id: null` | Remove the old `machine_id` key. The new credential is unbound. |
| No `machine_id` key | Preserve the prior binding for legacy response compatibility. Absence proves no new binding. |
| Invalid binding, token or public token metadata | Reject the response and leave the old config intact. |

Refresh rotates the access token and may return binding metadata; the server
currently copies the credential's binding into the rotated credential. This
client repair accepts its authenticated result without adding a server rebind
capability. Server registry and credential checks remain authoritative.

The receipt is local to one operation and consumed even if save fails. A changed
starting config, different token/response, foreign origin or wrong endpoint
causes a HOLD instead of overwriting newer state. No pending binding is retained
for a later unrelated update. Manual replacement of an existing binding remains
an error. Unrelated options survive authentication; explicit null refresh tokens
remove the saved refresh token. Private atomic persistence remains delegated to
the existing filesystem backend (0700 directory / 0600 file on POSIX, existing
ACL validation on Windows).

A revoked refresh may require device login again. The old config stays intact
until a valid replacement is saved, including when that re-pairing fails. Keeping
the old file does not make revoked credentials usable. Resolve authentication
with the operator; never edit a machine ID or bypass bridge qualification to
make a rejected credential appear bound.

An unbound credential or legacy response with stale metadata can still leave
the conversation bridge on HOLD. It must continue to require the exact current
participant, project, canonical machine, genuine native session/workspace and
conversation membership. Config metadata is not a replacement for these checks.

## Moving an already bound agent

There is no admin rebind endpoint. Approval can bind an existing unbound agent
to a machine, but changing an existing non-null binding returns
`409 agent_machine_conflict`. Posting an existing registration to
`POST /api/agents` returns `409 agent_exists`. Moving a bound agent therefore
requires controlled **DELETE + POST + re-pair**, with an interruption of its
credential lifetime.

1. **Prepare custody and rollback.** Name the current operations owner, source
   and destination host custodians, exact hub origin, project and tag, old and
   target machine IDs, approved scopes and maintenance window. Obtain their
   acknowledged handoff and Alexey's operations approval through root. Preserve
   private registration metadata, config and journal evidence through the
   approved private storage path. Keep credentials out of issues, inbox messages,
   terminal captures and committed files. Confirm a rollback owner and an
   approved route to re-pair on the old machine before deletion.
2. **Check identity and destination.** Use the current admin session to inspect
   `GET /api/agents` and the target machine's genuine registration. Select the
   exact `project:tag` actor, for example `example:worker`. A bare tag can be
   ambiguous across projects and return `409 ambiguous_agent_tag`; never guess
   which row to delete. Confirm destination host enrollment and the operator's
   access separately. Neither is established by these source tests.
3. **Fence current work with its owner.** Arrange an acknowledged pause/handoff
   of the affected agent's work, pending effects and obligations. Preserve native
   journals and uncertain delivery records. Do not reset a journal or retire any
   old service, timer or bridge as part of this config repair.
4. **Replace only the approved registration.** Through the current approved
   admin session, submit `DELETE /api/agents/<URL-encoded-project:tag>` and check
   the returned project and tag. Then `POST /api/agents` with `tag`, `project`,
   the approved `role` and target `machine_id`; expect `201` and the exact actor.
   Include the session's current `x-csrf-token` on mutations; device approval
   also requires the current admin session and CSRF protection. Use the existing
   approved admin workflow, with no credentials in example commands or logs.
   The registration API accepts `tag`, `project`, `role` and `machine_id`;
   legacy `workspace` and `session_id` SQL columns are not registration fields
   or proof of a genuine native binding.
5. **Re-pair and verify new credentials.** Start the existing agent device-login
   flow on the intended host. The operator reviews the exact project-qualified
   actor, requested scopes and target machine before approving with
   `expected_actor` and the approved `machine_id`. Verify successful login and
   private config save, then authenticate a read of
   `/api/conversations/participants`: its `self.actor` must be the intended
   agent and its participant registration must show the target machine. Verify
   genuine native identity and the bridge's qualification without bypasses.
   Confirm old access and refresh credentials are unusable through the approved
   private verification workflow; do not print them. Registry deletion revokes
   that actor's existing credentials and invalidates prior approved pairings.
6. **Accept or roll back.** Keep the agent held if any registration, permission,
   native identity or authenticated read differs. If rollback is authorized,
   coordinate the same replacement sequence back to the preserved old
   registration and re-pair there. Restoring the old config file cannot revive
   revoked credentials. Recheck conversation membership, pending effects and
   obligations before resuming work; actor-name reuse alone is not proof of
   continuity. Record sanitized results and retain private evidence with the
   acknowledged owner.

Local tests establish the config transition and failure behavior only. Main
integration, deployed runtime verification, real enrollment and physical
cross-host adoption remain separate acceptance gates under the platform plan.
