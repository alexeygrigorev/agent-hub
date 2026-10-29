# Hub host lifecycle

Hub checks enrolled, undrained hosts during every maintenance check, independently
of configured liveness checks. More than 900 seconds without checkin opens one
`host_watch` incident, keyed by machine id. A fresh checkin resolves it and emits
one `RECOVERED` notification. Drain alone never proves recovery. Delivery uses
the configured Hub alert inbox/webhook and retains receipts for retries.
Fleet and status coverage expose `last_checkin_at`.

An operator can set a durable bring-up goal using managed engine bundle labels:

```sh
hub host-goal --machine MACHINE --sha FULL_MAIN_SHA --workspace ADVERTISED_WORKSPACE \
  --engine shared-codex --engine shared-claude
hub host-goal --machine MACHINE
```

The API is `PUT /api/fleet/routing/host-goal/MACHINE` with
`{"sha":"40-hex-sha","workspace":"tasks","engines":["bundle-label"]}`;
GET reads progress. Only operators can set/read goals. Labels must already name
centrally configured bundles; the goal creates no account, credential or purchase.
Existing managed provisioning installs those bundles in user scope and reports
their exact revision. Checkout hosts consume the release pin through self-update.
Linux immutable release hosts report manifest provenance and use the bundled
release builder/switcher to build the target from a fresh `origin/main`. The
source checkout supplies Git objects and is never checked out, merged or reset.
`AGENT_HUB_SOURCE_REPO` can select its location (default `~/git/agent-hub`). The
target must be a fast-forward of the running release, from this project's
configured GitHub origin. The updater checks Hub's validated disk floor plus
1 GiB headroom and refuses a superseded target before activation.

Activation clones the current user-service configuration into the managed
`zz~~hub-desired-release.conf` drop-in, preserving credential/config/journal
paths. It reloads user units and uses the normal supervisor restart marker.
`updating`/`updated` retain the old running SHA until the new process reports
`current`; goals wait for that report. Hosts predating the bundled update kit
need the normal one-time host release rollout. Unsupported layouts and build,
disk or activation failures expose a bounded reason without inventing a SHA.

Hub maintenance places a verification session for each installed desired bundle
through normal fleet admission, preferring headless mode and using advertised
interactive mode when headless is unavailable. Quota, resource, workspace and claim checks remain
authoritative. A launch receipt does not complete the goal: a fresh matching native
running session on that host, with matching engine/account/profile/workspace,
proves startup. The retained proof includes launch id, native id and verification
time. A new bundle revision requires new proof. Failed starts remain visible and
do not cause unbounded automatic retry sessions.
After repairing a failed attempt, repeat the desired-state command with `--retry`
to create a fresh verification generation. Direct operator host-release pins
also update an existing goal's desired SHA and require fresh startup proof.

Windows self-update migrates the existing `run-session-host.ps1` supervisor task
in user scope and reports `autostart_state` to Hub. The same idempotent task
registration can be run explicitly without requesting elevation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows-host-autostart.ps1 `
  -Runner "$env:LOCALAPPDATA\agent-hub\run-session-host.ps1"
```

The task uses S4U (no stored password, no interactive desktop), limited privileges,
a persistent one-minute time trigger, `StartWhenAvailable`, battery support and
`IgnoreNew`. Its name is scoped to the current user's SID; it preserves legacy
tasks and records the chosen name beside the supervisor wrapper. It avoids BootTrigger, whose registration requires administrator
membership. A Windows policy denying S4U/batch logon is an explicit failure; the
installer never elevates or substitutes a logon-only task. A Linux fixture cannot
prove Windows scheduler execution: acceptance still requires a real reboot/wake
cycle and resumed checkin within two minutes on Tabo.
