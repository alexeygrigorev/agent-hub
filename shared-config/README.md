# Hub shared-config bundle (versioned, non-secret)

This directory is the versioned source of Hub-managed shared config that must
be identical on every Hub host (Hetzner and any new machine). The host agent
syncs it into the Hub-owned path `~/.config/agent-hub/shared/` (Windows:
`%APPDATA%\agent-hub\shared\`) via `hub shared-config sync` and on every
engines `sync()` tick (best-effort, never fails the sync).

## Current entries

- `aws/config` — AWS shared config. The default entry is the
  `default` profile for sandbox 817685572750 via AWS Gate
  (`credential_process = hub aws-gate credential-process`). NO static keys:
  this bundle must never contain `aws_access_key_id`,
  `aws_secret_access_key`, bearer tokens, URLs with credentials, or any
  `AKIA…` value. The Gate bearer token stays per-machine in the host-only
  `~/.config/aws-gate/env` (mode 0600), created by the AWS Gate install/pair
  flow, never copied into this repo.
- `manifest.json` — `{"version": N, "files": [...]}`. The sync copies exactly
  the listed relative paths; anything else in this directory is ignored.

## How it works at runtime

- Managed engine homes isolate `HOME`, so `~/.aws/config` is invisible there.
  `provisioning.engine_environment()` therefore sets:
  - `AWS_CONFIG_FILE` → Hub-owned `~/.config/agent-hub/shared/aws/config`
    (the synced copy of this bundle's `aws/config`, with `hub` resolved to an
    absolute path when discoverable at sync time);
  - `AWS_SHARED_CREDENTIALS_FILE` → a nonexistent file inside the managed
    home (`<managed-home>/.aws/credentials`), so per-machine static keys
    (e.g. `main-temp` in the real `~/.aws/credentials`) stay unreachable.
    `provision_managed_home()` never copies `~/.aws/credentials`.
- `hub aws-gate credential-process` (see `host/agent_hub_host/aws_gate.py`)
  resolves the real host Gate env WITHOUT trusting `HOME` (which is the
  managed home inside workers): it derives the host config base from
  `AWS_CONFIG_FILE` when that points under `agent-hub/shared/`, else from
  `HUB_HOST_HOME`, else from `Path.home()` only when that home actually holds
  `.config/aws-gate/env`. It then performs the Gate credential-process HTTP
  call (stdlib only, 10 s timeout) and prints the standard
  `{Version, AccessKeyId, SecretAccessKey, SessionToken, Expiration}` JSON.
  All failures are sanitized one-liners on stderr with exit 1; bearer tokens
  and URLs never appear in logs.

## How to add further shared config folders

1. Add the new files under `shared-config/<tool>/…` (non-secret only).
2. List every new relative path in `shared-config/manifest.json` (`files`).
3. If the consumer needs an env-var pointer (like `AWS_CONFIG_FILE`), extend
   `host/agent_hub_host/shared_config.py` (`SHARED_CONFIG_FILES`, plus a
   `hub_<tool>_…()` helper and the `engine_environment()` wiring in
   `host/agent_hub_host/provisioning.py`) with unit tests in
   `host/tests/test_shared_config.py`.
4. If the consumer needs host-release distribution, ensure `shared-config`
   stays in `scripts/build-host-release.py` `TRACKED_PATHS` and
   `scripts/host-release-manifest.json` `hashed_trees` (it already is).
5. Run the full suite (`scripts/test-full-local.sh`) and re-sync every host.

## Install on a new machine (dry-run tested)

1. Install the Hub CLI (no secrets):
   `curl -fsSL <hub-base>/cli/install.sh | sh` (Windows: `install.ps1`).
2. Create the per-machine Gate env (secret, host-only, never in this repo)
   via the AWS Gate install/pair flow so `~/.config/aws-gate/env` exists with
   `AWS_CONTAINER_CREDENTIALS_FULL_URI` + `AWS_CONTAINER_AUTHORIZATION_TOKEN`.
3. Sync (renders the absolute `hub` path into the Hub-owned copy):
   `hub shared-config sync`
   Dry-run against a temp HOME without touching the real host:
   `XDG_CONFIG_HOME=/tmp/fake-xdg hub shared-config sync --dry-run`
   (or without `--dry-run` to write under the temp base and inspect).
4. Verify from a managed-equivalent env:
   `AWS_CONFIG_FILE=~/.config/agent-hub/shared/aws/config`
   `AWS_SHARED_CREDENTIALS_FILE=/nonexistent-inside-managed-home`
   `aws sts get-caller-identity --profile default` must return account
   `817685572750`, and `--profile main-temp` must fail (unknown profile).

## Optional Gate MAIN profile

`aws --profile main sts get-caller-identity` chains from the unchanged default
Gate credential process to `arn:aws:iam::387546586013:role/aws-gate-main-access`
for 900 seconds. The phone grant must have “Also give access to MAIN” checked.
The vendor supplies the session tags required by MAIN trust; clients pass no
external ID or tags. MAIN has AdministratorAccess. Stop/expiry prevents fresh
Gate credentials; issued sandbox credentials remain usable for up to 15 minutes
and existing MAIN sessions last until their own expiry (role maximum one hour).
