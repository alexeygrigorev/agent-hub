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
- `release-manifest.json` — written by the build with the git rev and sha256
  of every required file, plus `.venv/` with a release-local interpreter.

`host/tests/test_release_completeness.py` scans host code for release-relative
reads (`parents[2] / ...`) and fails if any is missing from the manifest.

## Install-time smoke gate

`scripts/verify-host-release.py <release-dir>` checks every manifest file
(presence + sha256) and smoke-loads the release's own routing config by
importing `model_contract` and `native_admission` from the release and
loading policy models, routing rules and account aliases.

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
