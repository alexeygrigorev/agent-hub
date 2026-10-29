# Intake scope decisions

Scope policy v1 lives in `src/intake_scope.ts`: the project registry contains
`scope_terms` and `not_terms` for `agent-hub` and `pocketshell`, adjusted to the F1
charters. Hub may control any session, including manual PocketShell sessions.
Overlapping automation is removed from PocketShell; `pocketshell-mcp` is retired.
DataOps MCP is a separate product, with no Dapier/AISL exception.

Creating an intake always saves it subject to normal input/storage limits.
`scope_flag` is `in_scope` or `needs_decision`; `scope` shows policy version,
matched terms and the operator decision. Show, list, handoff, CLI and MCP expose
these fields. Existing tasks without scope metadata stay unchanged and ungated.
The classifier is a lexical hint, not a semantic scope judge. Research that
merely discusses a term is allowed; a mixed research/implementation request is
flagged. Unknown projects have no matching terms. Operators review ambiguous
requests against the project charter.

**Policy v2 (Alexey t1600u-t1602u, 2026-10-27): scope is informational only.** A
flagged task is never held: assign, linked session starts, the watchdog restart
and the verifier start proceed regardless of flag or decision, and the codes
`scope_decision_required` / `scope_out_of_bounds` are no longer returned. Flag,
matched terms and any recorded decision remain visible in show/list/handoff.
The text below describes the historical decision event, which stays readable
but is optional and never required to start work.
Ordinary task text, `decision` notes and agent claims cannot approve work.
An authenticated operator appends this event through the ordinary CAS endpoint:

```json
{
  "key": "scope-decision-unique-key",
  "expected_version": 1,
  "event": {
    "kind": "scope_decision",
    "verdict": "gray_adr",
    "text": "Alexey approved the linked ADR for this version.",
    "source": {"type": "chat", "reference": "SCOPE.md ADR with intake ID"}
  }
}
```

Use `POST /api/intake/tasks/<id>/events`, `hub intake append <id> --version 1
--key scope-decision-unique-key --event-file event.json` (event object only in
the CLI file), or MCP `intake_append`. Verdicts `in_scope` and `gray_adr` allow
work; `gray_adr` requires a source reference to the approved ADR. Verdicts
`out_of_scope` and `wrong_project` are recorded as history only (no refusal); refile or
move the project under an authorized CAS update before requesting a new decision.
Changing project requires write access in both source and destination.

Decisions bind to project, title, all intake text, acceptance criteria, policy
version and scope revision. Scope-relevant edits invalidate the decision even
if the content is later restored; ordinary progress notes do not. An old policy
version or invalidated decision requires a fresh operator decision. Updates
accept `project` via HTTP, CLI `--project` and MCP `intake_update`.

Project operator rules `scope-boundary-agent-hub` and
`scope-boundary-pocketshell` carry the adjusted NOT lists and instruct an agent
to stop and add a decision request instead of implementing work outside scope.
Only operators can mutate those rules or record scope decisions.
