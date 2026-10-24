# Automatic product boundary checks

Run `python3 scripts/ci/scope_guard.py --working` locally. The mandatory full
suite runs it too. The baseline defaults to fresh `origin/main`; CI push checks
use the event's `before` SHA so a multi-commit push cannot compare HEAD to itself.
Every new commit is inspected, including merge parents and `[skip ci]` commits,
plus the final diff. Existing code on main requires no exceptions.

Hub rejects new terminal/SSH dependencies, terminal transport and new WebSocket
constructs in runtime files. Bounded capture UI is allowed. Comments and docs
are excluded from token matching. This is a mechanical boundary check, not a
replacement for the ownership review in SCOPE.md.

`Scope: in` or `Scope: gray <intake-UUID>` is required in the final commit trailer
block for session control, registry, auth and public changes. Adoption starts
2026-10-24 UTC: missing trailers warn for one week and fail from 2026-10-31 UTC.

An exception is an exact `{path, token, intake_id, approved_by, date}` entry in
scope-exceptions.json. It must already exist on the base revision; candidate
exceptions are validated but cannot authorize code in the same checked range.
Use a separate approved intake to establish an exception before implementation.
Changes to SCOPE.md and exceptions still enter the guard's unfiltered commit scan.

PocketShell PR checks run trusted base-revision code against candidate Git
objects. A violation requires both `scope:gray` and a PR link to an already
approved `docs/adr/*.md` or `docs/decisions/*.md` on the base, with `Status:
approved`, `Approved-by: Alexey` and `Intake: <UUID>` on separate lines. A label
or a candidate ADR alone cannot approve itself. Manual session start and human
terminal access remain allowed; removed automation paths and MCP server code
cannot be reintroduced silently.

GitHub suppresses push/pull_request workflows for native `[skip ci]` directives.
The required local full suite always checks those commits. The standalone Hub
workflow has no path filter, and the deploy workflow checks scope before tests
and again before deployment, including emergency manual deployments.

Reviewers check entity ownership (tasks/AC/models/accounts/placement = Hub;
bytes/keys/device/human input = PocketShell; process/session record = aplexer),
new cross-product runtime calls, link alternatives, removed paths, independent
availability and a negative boundary test.
