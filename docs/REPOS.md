# Repository registry

Hub keeps the private repository map in its SQLite Durable Object. Each entry
has a unique name, label, aliases, per-host checkout paths, origin
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
`purpose`, `stack`, `key_folders`, `urls`, `status`, `canonical`, or `allowed_machines`. The latter is a
list of machine IDs; absent or `null` permits every enrolled host, and `[]`
permits none. For example, `{"allowed_machines":["HOST_ID"]}` restricts both
existing-checkout and clone routes. For example,
`{"status":"duplicate","canonical":"dtc-website"}` marks an entry as a duplicate.
The target must already exist and be active; self-links, missing/inactive targets,
and duplicate cycles are rejected without changing the entry. Setting status to
`active` or `archived` clears its canonical link. Edit/seed require an operator. A host can
refresh only checkout rows bound to its authenticated machine; another host may report the same repository. Authorized host and
agent readers can list/show. API routes are `GET /api/repos`,
`GET /api/repos/{name}`, `PATCH /api/repos/{name}`, `POST /api/repos/seed`,
`POST /api/repos/refresh`; list filters are `org`, `status`, `flagged=true`, `host`, `missing_on`.
Seed/refresh accept `{"repos": [...]}` in bounded batches. MCP tools `repos_list`,
`repos_show`, `repos_edit` use the same routes and authorization.

## Deployment readiness: green edge is not a usable duplicate API

Incident evidence (1 Nov 2026, intake 4c0bae31): GitHub Actions run
`41291900819` completed successfully for `6d0aa9d` at 09:27:06 UTC. The
operator reported that both Hub domain manifests advertised that commit, while
PATCH requests containing `status`/`canonical` still returned HTTP 400
`invalid_human_fields` on both domains until at least 09:30 UTC; a queried
PATCH and then an ordinary CLI PATCH succeeded at 09:33:22 UTC. The delay's
cause is unverified.

The source history explains what the error establishes. Before `6d0aa9d`, the
PATCH validator allowed only human metadata keys and returned
`invalid_human_fields` for `status`/`canonical` before it looked up a registry
row. In `6d0aa9d`, those keys became operator-editable and the validator's
error changed to `invalid_repo_fields`. With a valid body and a missing repo,
the updated handler proceeds to the lookup and returns `repo_not_found`.
Thus the observed `invalid_human_fields` is evidence that those requests hit
the pre-change API behavior. It does not establish why that behavior remained
reachable after the green run; no cache or propagation cause is known.

The deployment smoke in that run checked `/livez`, the CLI manifest's
`built_from_commit`, and the worker's JSON authentication response from
`/api/sessions`. These checks do not exercise
`PATCH /api/repos/{name}`. `/livez` is constant unauthenticated edge liveness;
the manifest is a static asset. Authenticated `GET /api/healthz` exercises the
Durable Object and returns a constant body,
`{"status":"ok","component":"hub-do"}`; it establishes authenticated DO
reachability, not that a particular repo API contract is active.

After a registry deploy, verify readiness before editing classifications:

1. Confirm both Hub domain manifests report the expected release commit.
2. Using the existing authenticated operator client, confirm
   `GET /api/healthz` returns 200 and the expected `hub-do` body. Keep the
   credential private.
3. Generate a fresh UUID repo name. First request
   `GET /api/repos/<probe>` and require 404 `repo_not_found`; stop if the row
   exists or the response differs. Send an authenticated
   `PATCH /api/repos/<probe>` with
   `{"status":"duplicate","canonical":"readiness-probe-target"}`.
   Require 404 `repo_not_found`, then GET the same name and require 404 again.
   This exercises the new field validator and missing-row lookup without
   creating, changing, or cleaning up any registry record. In particular,
   `invalid_human_fields` means the new API contract is not active. Treat any
   other unexpected result as not ready and investigate; do not proceed with
   production classifications.
4. Only after the probe passes, run ordinary `hub repos edit` classifications.

`test/repos.test.ts` pins the contract at the real exported Worker entry point
(`SELF.fetch` through Worker auth into the Durable Object): authenticated
health and unauthorized health, duplicate PATCH and canonical resolution,
refresh preservation, and a missing-row PATCH probe with no write.

## Session resolution

An exact repository name takes precedence over labels/aliases of other entries.
A registered name, label or alias resolves to its canonical repository before
Fleet placement. The logical name remains in the request until Fleet evaluates
each host's checkout rows and chooses an executable absolute path. Duplicate chains resolve to the canonical entry; loops and missing targets
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


## Multiple hosts

The catalog now uses `repo_id` (lowercase GitHub `owner/repo`, or `local/name`)
with a case-insensitively unique name. `repo_checkouts` stores each `(repo_id,
host_id)` path, status, HEAD, branch, dirty counts, free bytes and observation
history independently. Existing single-host rows migrate transactionally on
initialization; human fields and duplicate links survive. Distinct legacy entries
sharing an origin retain separate local identities during migration.

GET list/show and the JSON CLI output include `checkouts`, `checkouts_by_host`, `hosts`,
`present_online_count` and `no_present_online`. Checkout rows include both
`host_id`/`path` and `machine_id`/`local_path`. The compatibility `machine` and
`dir` fields represent a present checkout, preferring an online host. Present
means a clone exists; online means a machine check-in within three minutes.
The Fleet page's Repos section highlights zero present checkouts on online hosts.
An enrolled host's explicitly advertised repo workspace also supplies checkout
evidence when it has no scanned row. Windows drive paths are supported. A generic
workspace root does not imply that every repository is checked out beneath it.
`hub repos list --host HOST` filters present checkouts; `--missing-on HOST`
finds repositories without one there.

Refresh roots come from repeatable `--root` flags, then `repos.scan_roots` in
`~/.config/agent-hub/subscriptions.json`, then `workspace_roots` (excluding roots
named `worktrees` or `tmp`). Empty configuration fails with
`no_configured_repo_roots` before any HTTP request. There is no implicit
`~/git` fallback. The installed timer uses these settings without embedding a
path. All roots are scanned before submitting results so one root cannot mark
another root's checkouts missing. Git origin is reported as both `origin` and
`origin_url` (GitHub owner/repo); null or empty origin/default branch observations
never erase successful catalog metadata.

## Placement and guarded provisioning

Clone routes multiply the placement score by 0.85, giving existing checkouts a 15% preference at equal load. A host without one is
eligible only when the catalog's `clone_policy` is `clone_on_demand` (the default), a GitHub
`origin_url` is known, its cached `gh_authenticated` capability is true, and fresh disk
state for the checkout mount is `ok`. Missing or unknown prerequisites remain
visible as `repo_checkout_missing`, `repo_origin_missing`,
`host_gh_auth_unavailable`, or `host_disk_warn_no_clone`. These reasons alone do
not queue; they can accompany a valid host's transient capacity hold without
turning its HTTP 202 queue result into a hard rejection.

Cloning requires an explicit true `repo_clone_enabled` capability and a valid
`repo_checkout_root`. The host chooses the first writable configured scan or
workspace root, honors `repos.checkout_root` and `repos.clone_on_demand`, and
does not infer clone permission from legacy workspace aliases. Fresh mount
observations take precedence over a different filesystem's heartbeat warning.

The host probes `gh auth status --hostname github.com` at most once every five
minutes, retaining only a boolean and discarding output. Provisioning uses
`gh repo clone` into a sibling `<target>.tmp.<pid>` directory under an explicit
`workspace_roots` entry. A per-repository `.hub-clone-<name>.lock` serializes
cloners. Fresh filesystem usage and free bytes are checked under the lock before
cloning on checkout mounts; warning, critical inode or space pressure and
insufficient free space refuse cold clones.
Only a successful clone is published with `os.replace`. Existing targets, symlinks and pre-existing staging data are never overwritten.
The invocation removes only its own new failed staging directory.
Successful clones immediately refresh their authenticated host checkout row,
including the cache marker and server-recorded last-use time. Ordinary scans
retain that marker at the same path. No automatic cache eviction is enabled.

`repos` accepts `scan_roots`, `checkout_root`, `worktree_root`, `clone_on_demand`,
`clone_filter` (`blob:none` or null), `clone_timeout_sec` (1–900), and
`max_cached_clones` (metadata for future cache management).

POSIX clone effects require `flock`, `statvfs`, secure directory-open
flags, and process-group cleanup. Windows cloning uses a persistent byte lock,
a handle that refuses reparse points, and a fresh free-disk check. Windows hosts
advertise no repository worktree root: enabling cloning does not enable POSIX
worktree effects. Hosts without the required platform APIs advertise cloning
disabled; existing checkout launches remain available. Stale unsupported requests
fail with a typed refusal. Clone errors discard command output.

Both repo names and registered raw checkout paths receive repository metadata in
their launch prompt. A raw-path request retains its exact workspace; operator host restrictions still apply.

The 0.85 cold-checkout score factor is a soft preference: at equal quota and
load, an existing checkout wins, but healthier quota can select a cold host.
