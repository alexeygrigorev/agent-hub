# Session workspace binding repair

Intake: `5f38de5e-f164-4c0e-a360-59f743905d4e`.

The native runtime owns workspace movement and native incarnation. Hub consumes
its catalog; Hub must not rewrite native identity or borrow another credential.

## Failure mechanism

The launch binding is recorded once. The default host-bindings query selects
only sessions with a null binding, so an already bound session disappears from
reconciliation. `applyBinding` also rejects any changed workspace as
`binding_conflict`. A later native workspace move therefore leaves the original
Hub workspace in place. Delivery qualifies the native target by tag and
workspace; the old pair no longer addresses the moved session.

## Required repair contract

The assigned host reports its fresh native catalog first. Only that host's
authenticated binding route may repair a workspace. Keep the UUID, tag and
native creation timestamp unchanged; verify all three against the fresh
catalog. The new workspace must be an active registered worktree belonging to
the session's existing intake on the assigned machine. Preserve the existing
Fleet launch, engine and host-family checks. Increment the Hub generation and
revoke old messaging credentials on success. A claimed command from an older
generation must never acquire authority over the new binding.

The policy helper in `src/session-binding-move.ts` checks native incarnation,
catalog freshness and same-intake worktree ownership. The host binding route
uses it inside the mutation transaction. Host discovery also uses it to find
eligible moves. Existing execution custody blocks repair; unclaimed commands
receive a fresh generation and correlation before being offered again.

## Interactive throwaway ownership

Worker credentials permit same-intake headless child starts only;
`mode: interactive` returns `403 worker_scope_forbidden` (`mode_not_headless`).
Keep that authorization boundary. An authenticated operator owns the throwaway
interactive Codex start, then verifies readiness, useful action and confirmed
stop. A second session cannot take over an intake worktree protected by its
live owner (`dispatch_worktree_refused`, verified by Hub session
`2ce65ae6-3927-4f8a-8bf5-3eef8aa2b038`); preserve that refusal and the owner.

Use the existing unlinked interactive operator/debugging facility in the
registered main workspace, with an empty prompt and no source edits. For
example, an operator can run:

```sh
hub sessions start --workspace agent-hub --engine codex \
  --reason 'Binding repair: operator-owned debug throwaway; no source edits' \
  --interactive --name binding-move-debug --key binding-move-debug-v1
```

This debug intent omits `--intake-task-id`; it does not claim the original
intake's occupied worktree. Attach the canonical session ID and readiness and
confirmed-stop receipts as notes on the original intake (proven by Hub session
`7ea0f127-c964-4e59-8dac-069ffd7c4cb0`, native
`13864c56-dfb6-4e44-affb-9dd1772a0717`, tag
`hub-d0bf13dc-40cb-4d40-9ce5-6bc951bd10a3`). Keep normal routing, quota,
resource and host protection gates.

Do not substitute host credentials for operator start authority or override
native identity.

## Acceptance evidence

Completion requires regression tests through the real binding HTTP route,
independent review of the exact source, the full local suite, integration,
green required workflows and live repair/wake receipts. A pure policy test
alone does not satisfy these criteria. Preserve the open intake and worktree
until every current criterion has evidence. Deployment remains subject to the
repository's explicit no-deploy rule.
