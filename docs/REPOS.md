# Repository registry

Hub keeps the private repository map in its SQLite Durable Object. Each entry
has a unique name, label, aliases, owning machine, absolute directory, origin
(`org/repo`), org, purpose, stack, key folders, workflows, URLs, default branch,
active/archived/duplicate status, canonical duplicate target, last commit SHA/date,
and discovery flags. Repository paths and descriptions are private authenticated
data; do not check a seed or raw registry output into Git.

## Seed and daily refresh

Use the operator CLI profile for the one-time seed:

```sh
hub repos seed --machine MACHINE_ID --root "$HOME/git" --file "$HOME/tmp/projects-map-2026-10-12.md"
```

The importer reads project tables, duplicate/related notes, less-active groups
(including `python-course(-code)` expansion), and human aliases. It skips the
scratch group and session/engine listings. The combined Purpose / stack cell is
preserved verbatim in both fields; it is not split by heuristics. Human fields
retain Markdown/backticks and annotations. Re-seeding leaves existing entries
unchanged. Duplicate entries point to the preferred directory in the map.

```sh
hub repos refresh --machine MACHINE_ID --root "$HOME/git"
scripts/install-repos-refresh.sh MACHINE_ID
systemctl --user status agent-hub-repos-refresh.timer
journalctl --user -u agent-hub-repos-refresh.service -n 100 --no-pager
```

The user timer runs daily with persistent missed-run catchup. It invokes the
installed `hub` binary, so ordinary CLI updates also update the refresh code.
The service uses the existing host profile and never obtains credentials.
Refresh scans immediate children of the configured root, skipping symlinks and
`.git` files (worktrees). It uses Git for origin/last commit, lists YAML workflow
files, and invokes only `gh repo view ORG/REPO --json defaultBranchRef,isArchived,url`
for GitHub metadata. GitHub login remains entirely managed by `gh`.

NEW entries remain flagged. A single missing canonical directory rediscovered
under the same origin is marked MOVED and its directory updated. Missing entries
remain stored with MISSING; a former clone now represented by a worktree also
has WORKTREE. Git/GitHub errors have GIT_ERROR/GH_ERROR, retain old successful
metadata, and are retried next run. Logs contain counts and flagged names.
Human purpose, stack, key folders, URLs, label and aliases are never sent by
refresh. The machine-derived GitHub URL is separate from human URLs.

## CLI, API and MCP

```sh
hub repos list
hub repos list --org DataTalksClub --status active
hub repos list --flagged
hub repos show dtc-website
hub repos edit dtc-website --file human-fields.json
```

The edit file is a JSON object containing any of `label`, `aliases` (string array),
`purpose`, `stack`, `key_folders`, `urls`. Edit/seed require an operator. A host can
refresh only entries owned by its authenticated machine. Authorized host and
agent readers can list/show. API routes are `GET /api/repos`,
`GET /api/repos/{name}`, `PATCH /api/repos/{name}`, `POST /api/repos/seed`,
`POST /api/repos/refresh`; list filters are `org`, `status`, `flagged=true`.
Seed/refresh accept `{"repos": [...]}` in bounded batches. MCP tools `repos_list`,
`repos_show`, `repos_edit` use the same routes and authorization.

## Session resolution

An exact repository name takes precedence over labels/aliases of other entries.
A registered name, label or alias resolves to its canonical repository before
Fleet placement, pins its owning machine and supplies the canonical absolute
path. Duplicate chains resolve to the canonical entry; loops and missing targets
are refused. Existing configured workspace labels and explicit absolute paths
remain supported. Once seeded, unknown bare names return `unknown_workspace`
with the nearest registry names. No filesystem name fallback is used for them.
Fleet capabilities and host `workspace_roots` confinement still apply, including
symlink resolution. Registry metadata never grants filesystem access.

For a task-bearing start, the Fleet prompt receives a short JSON repo summary:
origin, purpose, stack, key folders, workflows, URLs and default branch. The
original session request remains the idempotency key input; metadata is appended
to the execution brief. Empty interactive starts keep their empty-prompt contract.
The summary is explicitly metadata, never executable instructions.
