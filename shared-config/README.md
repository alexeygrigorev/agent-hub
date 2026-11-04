# Hub shared-config bundle (intake 0043dadb)

Versioned, **non-secret** tool config Hub distributes to every host. Managed
sessions run with a private HOME, so a machine's own `~/.aws/config` (or any
other per-user config) is invisible to them by design; this bundle is the
single source for the config they must share.

## Layout

- Source of truth: this directory, `shared-config/<folder>/`, versioned by git.
- Host copy: `<agent-hub config dir>/shared/<folder>/` (Hub-owned, e.g.
  `~/.config/agent-hub/shared/aws/config`).
- Sync runs on the Hub installer/updater path (`provisioning.sync`: host
  startup and every machine heartbeat) and manually via
  `hub shared-credentials sync`.
- The version digest over synced bytes is recorded in
  `<shared>/config-state.json`; an unchanged bundle is not rewritten.
- Shared **credentials** (gh CLI auth, shared git SSH key) live beside this in
  `<shared>/gh` and `<shared>/git-ssh`; see
  `host/agent_hub_host/shared_credentials.py`. Nothing is ever written into a
  user's own `~/.aws`, `~/.ssh` or `~/.config/gh`.

## First entry: AWS Gate (profile `default`)

`shared-config/aws/config` carries one profile, `default`, whose
`credential_process` authenticates through AWS Gate (sandbox 817685572750).
There are **no static keys**. The file template holds a
`{aws_gate_credential_process}` placeholder; each host substitutes it from its
own non-secret `aws_gate_credential_process` setting in the host
subscriptions file (a command line such as
`python3 <path>/install_aws_gate.py credential-process --env <gate env>`).
A host without the setting **skips** the folder (fail closed) instead of
installing a default profile that cannot authenticate.

Session isolation (engine_environment): when the synced config exists,
launches set `AWS_CONFIG_FILE` to the Hub-owned file and
`AWS_SHARED_CREDENTIALS_FILE` to `<managed home>/.aws/credentials`, a path
that never exists, so any static-key lookup (e.g. `main-temp`) fails closed
instead of reaching the machine's own credentials.

## Adding a further shared folder

1. Create `shared-config/<folder>/` in the repo with the config files
   (UTF-8 text, each ≤ 64 KiB). Optional `{placeholder}` tokens are allowed.
2. Register it in `SYNC_FOLDERS` in `host/agent_hub_host/shared_config.py`,
   mapping any placeholder to its host setting key (settings allowlist lives
   in `fleet.load_settings`).
3. If a session environment variable should point at it, wire it in
   `engine_environment()` (host/agent_hub_host/provisioning.py) the way
   `AWS_CONFIG_FILE` is wired, with a fail-closed default.

## Install on a new machine

1. Install the hub CLI and enrol the host as usual (bootstrap registers the
   machine; `provisioning.sync` runs on startup).
2. Set the host setting, then sync (or just wait for the heartbeat):

   ```sh
   # in the host subscriptions JSON: "aws_gate_credential_process": "<command>"
   python3 -m agent_hub_host shared-credentials sync
   python3 -m agent_hub_host shared-credentials status   # non-secret summary
   ```

3. Dry-run check (writes nothing):

   ```sh
   python3 -m agent_hub_host.shared_config  # module API: sync_shared_config(dry_run=True)
   ```

   The test suite exercises exactly this against a temporary HOME.

## Supported operations and limits

- Sync, version digest, per-folder skip/partial statuses; never deletes user
  files and never carries secrets.
- Files must be UTF-8 text ≤ 64 KiB; no nested-directory depth limits beyond
  the path bound; binary files are refused.
- Distribution of the *credential* kinds (gh token, git SSH keypair) is a Hub
  admin operation (`POST /api/shared-credentials/<kind>`); this bundle is
  non-secret config only and must never contain one.
