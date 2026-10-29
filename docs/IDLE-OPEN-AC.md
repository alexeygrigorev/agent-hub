# Idle sessions with unfinished intake work

Hub links adopted native sessions using explicit intake links or an unambiguous
`START:` note containing the exact native ID or qualified tag. An operator can
link manually with `hub sessions link SESSION --intake-task-id INTAKE --key KEY`;
the equivalent REST route is `POST /api/sessions/SESSION/link` and MCP tool is
`sessions_link`. Linking never grants native-control authority.

A running session with a linked open intake becomes `idle_with_open_ac` when
native progress exceeds `HUB_AGENT_STALL_S`, or its observed Codex goal is
blocked, paused or stalled. A future scheduled wake suppresses recovery until
due. The operator status `needs_attention` projection and dashboard show the
session, intake, idle duration, native activity time and last nudge. Each new
episode appends an intake note.

The existing minute alarm and root tick run the durable recovery service.
`HUB_IDLE_OPEN_AC_NUDGE_MINUTES` defaults to 30. Nudges use the canonical verified
send/ack path and include the intake ID, full acceptance criteria and latest
note, with `/goal resume` for goal sessions. Each session receives at most one
nudge per hour. Interrupted enqueue attempts retry the same key and saved prompt
on the next eligible tick without consuming another nudge. After each nudge, Hub allows 30 minutes for both a verified ack
and native progress. Two unsuccessful nudges permit replacement through normal
placement admission. The replacement is interactive, goal capable, linked to
the same intake and workspace, and records parent lineage. The old session is
stopped only after a fresh census confirms the exact replacement binding is
running. Stale host observations never authorize native effects.

Controllers may register a recovery owner: `skip` suppresses effects for
observed or historical drivers; `managed` shares nudge eligibility but delegates
replacement and generation fencing to the controller. There is no second
unverified delivery path.

Resource watchdog and orphan cleanup fail closed for linked unfinished work.
Idle reaping remains allowed without an intake, after an intake `DONE` note, or
after fresh replacement-running proof. Missing intake or census evidence does
not grant permission. Explicit operator actions retain their existing authority
checks and protections.

Native recovery requires a progress `sampled_at` timestamp no older than three minutes. A fresh catalog heartbeat alone cannot authorize a nudge or replacement. Hosts without this sampling field remain protected until their normal release upgrade. Idle reaping checks every canonical alias and retained intake history link for the same machine/native identity; ambiguous or corrupt linkage fails protected.
