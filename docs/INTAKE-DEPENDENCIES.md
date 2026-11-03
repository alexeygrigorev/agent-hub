# Blockers and intake dependencies

Keep the original request intact when work cannot proceed. Report a concrete
follow-up with acceptance criteria:

```sh
hub intake blocker ORIGINAL --title 'Build tablet binary' \
  --text 'The installed binary does not support this architecture.' \
  --acceptance-criteria 'The native binary launches on the tablet.' \
  --key tablet-binary-blocker
```

This operation creates the follow-up, links the prerequisite and marks the
original blocked in one transaction. Reusing the same key and payload returns
the same follow-up. Link an existing prerequisite with
`hub intake depend ORIGINAL --on PREREQUISITE`. Remove an erroneous link with
the same command plus `--remove`. Cycles and cross-project links are refused.
CLI commands fetch the current version unless `--version` is provided; stale
explicit versions fail without mutation.

The API exposes POST `/api/intake/tasks/ID/blocker` with `key`,
`expected_version`, `title`, `text`, and `acceptance_criteria`; POST
`/api/intake/tasks/ID/depend` accepts `on` and optional `remove` instead of
the follow-up fields. MCP provides `intake_blocker` and `intake_depend`.
Task views and the intake UI show `depends_on`, unresolved `blocked_by`,
and prerequisite titles and completion state.

Both direct session starts and intake assignments refuse unresolved
prerequisites, returning `intake_dependencies_unresolved` and blocker IDs.
An operator can explicitly override this with `hub sessions start --force`
or `hub intake assign --force`. Workers cannot override dependency gates.

Finish a prerequisite by appending a current DONE note containing a commit
SHA or deploy-run URL, `Live check: ...`, and `Result: ...`. Do not close or
delete the intake. Changed requirements invalidate older DONE evidence.
Missing and cancelled prerequisites remain unresolved. The existing intake
watchdog checks every 30 minutes, releases dependency-owned blocking once all
prerequisites are DONE, and submits the original through canonical admission.
It preserves prior machine constraints, quota/resource checks, scope decisions,
approval obligations, and live-session duplicate protection. Unknown host
liveness is insufficient evidence to launch another worker.

Use `hub intake decompose ID --key KEY` to queue a deep-tier agent that
creates child intakes with acceptance criteria and prerequisite links.
It uses the existing session start and model policy; a queued intent is not
proof that decomposition ran. Optional `--workspace` selects a registered
workspace when it differs from the project name.

Hub-issued session credentials grant intake creation, reading and reporting
only in the linked project, with their existing expiry and refresh lifecycle.
They do not grant operator decisions, intake closure, host management or
cross-project access. Never print credentials. The standing launch prompt
requires reporting blockers rather than silently stopping. The watchdog's
forgotten-work projection flags a terminal session without current DONE
evidence or a linked blocker intake as
`session_ended_without_done_or_blocker_intake`.
