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
the same command plus `--remove`. Cycles are refused. Hub1 refused
cross-project links; Hub2 accepts them (see below).
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

## Hub2 prerequisites (`hub intake depend`)

On Hub2, `hub intake depend DEPENDENT --on PREREQUISITE` records the
prerequisite on the dependent intake. The prerequisite may live in another
project; the caller must be able to see it. Without `--version` the CLI reads
`task.control.version` from `hub intake show`. `hub intake show` lists the set
as `intake.depends_on`; `hub intake links ID` returns it as `depends_on`.
Hub2 records the link and shows it. It does not yet hold a start until the
prerequisite is done. `--remove` matches the stored id and skips the
visibility and cycle checks, so a stale link can always be removed. The
cycle check fails closed (`dependency_check_unavailable`) when a project in
the chain cannot be read. Two opposite links posted at the same moment can
still both land; remove one of them.

Every refusal names its cause: `prerequisite_required`,
`prerequisite_not_found` (missing, or not visible to this caller),
`prerequisite_ambiguous`, `dependency_self`, `dependency_cycle`
(cross-project walk, bounded to 64 intakes), `dependency_limit` (32),
`dependency_not_found` (on `--remove`), `intake_version_conflict` (the detail
gives the current version), `intake_closed`, `intake_not_found` and
`idempotency_conflict`.

## Intake PNG artifacts (`hub task-artifact`)

`hub task-artifact INTAKE --name NAME --file shot.png --sanitized` attaches a
reviewed PNG to an intake. `hub artifact INTAKE NAME --output out.png`
downloads it to a new mode-0600 file. Limits: 6 MiB (6,291,456 bytes) per PNG,
16 PNGs and 48 MiB per intake, 512 MiB per project. 6 MiB keeps the base64 download under the CLI's
10 MiB response cap. Callers: operators, project writers, or the session bound
to the intake. `hub intake show` lists name, bytes and sha256. The pixels never
appear in history pages.

A local refusal names the failed check: `PNG rejected (size check)`,
`(symlink check)`, `(regular-file check)`, `(type check)` (PNG
signature/IHDR), `(exists check)`, `(readable check)`, `(name check)` or
`(output check)`. Hub refusals keep their slug and detail. For example,
`artifact_too_large` with `size check failed: …`, `invalid_png` with
`type check failed: …`, `sanitized_attestation_required`,
`artifact_name_conflict` or `artifact_limit`. File mode is not checked.
