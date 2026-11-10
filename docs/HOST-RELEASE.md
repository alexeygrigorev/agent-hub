# Host releases

A host release is the self-contained tree a `agent-hub-session-host.service`
drop-in points at (`~/.local/share/agent-hub/host-releases/<git-sha>/`).
Every install path — `scripts/build-host-release.py`, any TBM or other
drop-in installer, and hand assembly — must produce the same complete layout,
or dispatches fail with `dispatch_local_command_failed` / `dispatch_route_*`
after the switch.

## Required layout

`scripts/host-release-manifest.json` is the single source of truth:

- `config/model-routing.json` — policy models, routing rules, account aliases.
  Read at runtime by `host/agent_hub_host/model_contract.py` and
  `native_admission.py` (with a dev-checkout fallback to the repo `config/`
  dir, which is why a slim tree without `config/` breaks).
- `scripts/session-host-supervisor.py` — the drop-in `ExecStart` entrypoint.
- `scripts/metrics-dashboard-export.py` — read by `metrics_producer.py`.
- `pyproject.toml`, `uv.lock` — build provenance.
- `host/` — the `agent_hub_host` package.
- `host/agent_hub_host/heavy_test_admission.py` — versioned shared heavy-test
  admission implementation, selected by worktree wrappers only after matching
  the active service release manifest and module hash.
- `release-manifest.json` — written by the build with the git rev and sha256
  of every required file, plus `.venv/` with a release-local interpreter.

`host/tests/test_release_completeness.py` scans host code for release-relative
reads (`parents[2] / ...`) and fails if any is missing from the manifest.

## Install-time smoke gate

`scripts/verify-host-release.py <release-dir>` checks every manifest file
(presence + sha256), requires the shared heavy-test module, and smoke-loads the
release's own routing config by importing `model_contract`, `native_admission`,
and the supported heavy-test admission protocol from the release.

`scripts/capped-test-run.sh` asks the active user service for its `ExecStart`,
runs the release's own verifier, requires `release_rev` to contain d8740aac,
checks the packaged module hash, then executes that module directly (no wrapper
recursion). It recognizes both the standard `host-releases` root and the
orchestrator's `host-releases-orch` root under
`~/.local/share/agent-hub/`; `AGENT_HUB_HOST_RELEASES_DIR` may add an explicit
managed root. If the active release predates d8740aac, the worktree script is
the fallback. `scripts/test-affected-local.sh` holds with a rebase instruction
before taking a slot when its worktree predates that revision; rebasing is
voluntary.

The host session loop also watches its shared admission pool. After a 10-second
zero-CPU sample confirms a queued admission is nested under a slot holder, and
only when no test runner is present in that process tree and the nested command
does not hold a second slot, it sends SIGTERM to the queued command's process
group and appends a sanitized note to the linked intake. It never unlinks lock
files or signals an admitted test process.

Existing worktrees are never rewritten by the host release switch. Until each
worktree rebases, old one-off test commands that bypass
`scripts/test-affected-local.sh` still use their local legacy gate. The host
reaper covers visible Linux process trees that hold a shared-pool ticket; a
process hidden from the host's `/proc`, a nonstandard admission command name,
or a test runner not recognized by the conservative active-runner list may
remain outside that protection. Rollout should prompt owners to rebase and
skip dirty, unpushed, locked or live worktrees rather than forcing updates.
New Hub session prompts check for d8740aac before gating, honor the HOLD, and
require a clean checkpoint plus head-coordinated non-forced rebase for an
older worktree. No automatic or mass worktree rewrite is part of the release.

Measured residual on RMTHZ, 2026-11-03 (~06:25Z, read-only ancestry scan of
521 `agent-hub-wt-*` checkouts, nothing modified): 34 contain 366aff60
(shared admission + reaper era), 74 more contain 0a353095 (priority-coercion
era), 13 more contain d8740aac, and 400 predate d8740aac. Checkouts before
0a353095 transiently refuse (exit 3, shared state untouched) while a foreign
`release`-priority row is queued; the row clears when that ticket admits
first, so no edit is required for this. Checkouts before 9f5ea5f2 ignore the
live slots file (compiled default 2; live file read 4, zero processes
exporting `AGENT_HUB_HEAVY_TEST_SLOTS`), so until they rebase they contend
for fewer shared slots — skew, not corruption. The active host release
(069dceb8) predates the shared module, so new-wrapper dispatch currently
falls back to the worktree copy and the host reaper awaits the next Hub
host-release rotation.

`scripts/switch-host-release.py` refuses to write any drop-in unless the
release verifies: it clones `--template` (normally the active drop-in),
substitutes the full old release directory path with the new one (refusing
templates that do not reference exactly one release dir), reloads user units,
and — with `--restart` — restarts the service and waits for `active`.

## Relabel-safe account routes

`fleet.Dispatcher._route()` matches the offered account by canonical identity:
`config/model-routing.json` `account_aliases` maps each canonical label to
its legacy aliases (`shared-go` <- `urgent-muse`), mirroring the server
`LEGACY_ACCOUNT_LABELS` ingest map (parity enforced by
`test/account-aliases.test.ts`). Listing both sides of an alias in one
settings file is rejected at load, so a relabel is an atomic one-label flip.

Enforced order for a relabel (checked by `scripts/check-host-routes.py`
against a `hub fleet` snapshot and the candidate settings file):

1. add the canonical route to the Hub registry (both labels route safely),
2. flip the host subscription label,
3. remove the legacy registry route.

`scripts/check-host-routes.py --settings <subscriptions.json>
--fleet <hub-fleet.json> --machine <id>` exits 1 on any registry route with
no canonical host match, and also exits 1 when the machine has no registry
routes at all (a typo'd `--machine` must not report success).

## Dispatch-route failure alert

The `dispatch_route` scheduled check (seeded by default, every 300s) fails
when more than `max_consecutive` (default 3) consecutive pre-start dispatch
failures with the same reason occur inside `window_s` (default 900s), tracked
both globally and per route (`machine/engine/account`) so a healthy route
cannot mask a broken one. Pre-claim `queued`/`offered` rows are skipped; any
other non-matching outcome breaks the streak. The failing check opens a Hub
alert delivered to the operator inbox, so a broken route class pages within
minutes instead of after an hour. Only the listed params are accepted for this
check kind, and other kinds reject them.

## Concurrency advertisement

`Dispatcher.once()` PUTs routing capabilities only when the loaded settings
carry an explicit `max_agents`. A settings state without it (mid-relabel
edit, generated file, degraded load) skips the PUT so the server keeps the
last advertised value; it never advertises a defaulted `1`. A failed settings
load never reaches the PUT at all (startup raises, reloads keep last good).

## Checkout-installed hosts: Hub-pinned self-update

Linux immutable releases built with the desired-state update kit also consume
the Hub pin automatically. They build from Git objects in `AGENT_HUB_SOURCE_REPO`
(default `~/git/agent-hub`) using `build-host-release.py --source-repo`, verify
the new tree, and switch the user unit through the normal restart marker. The
source checkout and its dependencies are left intact. The kit is bundled and
hashed in the release manifest. The managed final drop-in is
`zz~~hub-desired-release.conf`; later conflicting overrides are refused.
Status retains the running SHA during `updating`/`updated` and changes to the
new SHA only when the new release reports `current`.

### Operator switches and legacy overrides

For a running release that predates the migration kit, build a verified release
containing this fix, then run **that new release's switcher**, not the old running
release's script. During an approved maintenance window:

```sh
python3 <new-release-dir>/scripts/switch-host-release.py \
  --release <new-release-dir> \
  --template <active-legacy-drop-in> \
  --dropin <unit-drop-in-dir>/zz~~hub-desired-release.conf --restart
systemctl --user show agent-hub-session-host.service --property=ExecStart --value
```

The switcher and desired-state updater share the same managed filename and
ownership marker. They refuse an unmarked existing destination. Do not add
more tildes to override precedence. The switcher inspects both systemd's
DropInPaths and files on disk before mutation, so the bootstrap works even
when the old running host cannot migrate its own overrides.

During a verified host-release switch, the managed
`agent-hub-fleet-host.service` companion is set to the separately installed
Hub2 `heartbeat` command. Its timer refreshes the authenticated machine
heartbeat without fetching offers or reconciling native sessions. The Hub1
quota companion still follows the release for legacy deployments, and its
timer remains disabled on Hub2-managed hosts.

Both paths recognize legacy `zz~<7-to-40-hex-sha>-switch-release.conf`
names (including repeated tildes) only for regular, user-owned files in the
unit's user drop-in directory, referencing a single manifested release whose
SHA matches the filename. They preserve these as `.conf.disabled` before
reloading the unit; foreign later overrides cause refusal.

The updater holds `.hub-release-migration.lock` in the unit drop-in directory
through fetch, build and activation; the switcher inherits that lock or acquires
it for a manual invocation. Contending migrations refuse with
`release_migration_busy`; retry after the other migration finishes. Source
identity and content are revalidated after the build and before retirement.
Existing backups, including ones appearing during build, cause refusal without
replacing them. Linux `renameat2(RENAME_NOREPLACE)` also prevents a backup
appearing at the final rename from being overwritten. Unsupported rename
operations fail closed. Operators editing drop-ins should use the same lock
with `flock`, and never edit them while a migration holds it.

Before each retirement the switcher fsyncs a source/backup record to
`<releases-dir>/.host-release-switch-journal.jsonl`. For rollback, inspect this
private journal and the preserved overrides, restore a recorded backup only
when its source name is absent, then reload user units and restart during an
approved maintenance window. No override is deleted. After bootstrap, verify
the next Hub pin reaches `host_release.state=current` and the unit's ExecStart
references that same SHA. Build success and an active service alone do not
prove live acceptance.

Hosts that run the session host straight from a git checkout and have no
inbound channel (Tabo: Windows, no ssh) take releases from the Hub instead
of from an operator at the keyboard (intake a3227276):

```sh
hub host-release --machine cBynxGDoKVPoGpGWxvCTvg --sha <full main SHA>
hub routing-machines   # host_release: desired_sha, current_sha, state, error
hub host-release --machine cBynxGDoKVPoGpGWxvCTvg --clear   # stop pinning
```

The Hub stores only the target SHA. Every 120 s the session host
(`host/agent_hub_host/self_update.py`) reads its own pin and, when it differs
from `HEAD`, requires: the checkout's own `.git` (Linux release trees use the
bundled update kit above), branch `main`, no tracked changes (untracked files are kept),
the SHA reachable from a fresh `origin/main`, and a fast-forward of `HEAD`.
Anything else reports `refused` with the reason and changes nothing. It
then fast-forwards, runs `python -m pip install -e .` in the service
interpreter and an import smoke test; failure restores the previous commit
(`git reset --keep`) and reports `failed`. Success reports `updated`,
writes `host-release-restart` in the journal and exits the controller; the
supervisor exits with 75 and the service (Scheduled Task wrapper or systemd)
restarts everything on the new code without a crash alert.

The first run of a host that predates this feature needs one manual
bootstrap on the machine: `scripts/windows-host-update.ps1` (see
[DEPLOY.md](DEPLOY.md#windows-tabo-session-host-arm64-under-x64-emulation)).

## Windows package installs (immutable release trees): supported update path

Hosts installed via `irm https://hub.dtcdev.click/host/install.ps1 | iex`
(see [HOST-INSTALL.md](HOST-INSTALL.md)) run from an immutable release tree
(`%LOCALAPPDATA%\Programs\agent-hub\releases\<16hex>-<12hex>`), not a git
checkout (intake c8908c3e; LAPTOP-PHA6TCNC). For these hosts:

- `hub host-release --machine <id> --sha <sha>` triggers an immediate package
  update via `host/agent_hub_host/package_update.py` (intake e3e6bade). When
  `desired_sha` differs from the running release SHA, `self_update.py:step_once`
  delegates to `package_update.apply_host_release`, bypassing the 12 h spacing
  (`MIN_INTERVAL_S`) once for that request while preserving the 6 h per-archive
  retry bound (`RETRY_S`), the 50 GiB disk floor (`DISK_FLOOR_GB`), and any
  active operator hold (`package-update-hold.json`). While the per-user update
  task (`AgentHubUpdate-<SID>`) is scheduled or running, `host_release.state`
  reports `updating` and transitions to `current` once the new release starts.
- Automatic updates also run via the served `hub.pyz` archive
  (`host/agent_hub_host/package_update.py`, intake bf0863c4), checked from the
  same host-release loop: when the served `hub.pyz.sha256` differs from the
  running release's `runtime-ready.json` `archive_sha256`, the host registers a
  one-shot per-user scheduled task (`AgentHubUpdate-<SID>`) that reruns the
  public installer (no admin, no UAC). The installer verifies the archive
  checksum, builds a new release beside the old one, and hands the controller
  over, exactly as a manual rerun does. One attempt per served archive per
  6 h (`RETRY_S`), at least 12 h between automatic updates (`MIN_INTERVAL_S`),
  50 GiB free required (`DISK_FLOOR_GB`); a bounded operator hold
  (`package-update-hold.json`, at most 72 h) skips the installer via preflight
  exit 75. Manual path: rerun the same install command on the machine.
- Verify via `hub routing-machines` (`host_release.current_sha`,
  `state=current`) and the `package_update` event
  (`current`/`scheduled`/`pending`/`held`/`disk_low`/`served_unknown`).
