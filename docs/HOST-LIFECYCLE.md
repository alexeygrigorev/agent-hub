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
Immutable release hosts report manifest provenance and an explicit
`immutable_release` refusal if their release differs from the requested pin.

Hub maintenance places a verification session for each installed desired bundle
through normal fleet admission. Quota, resource, workspace and claim checks remain
authoritative. A launch receipt does not complete the goal: a fresh matching native
running session on that host, with matching engine/account/profile/workspace,
proves startup. The retained proof includes launch id, native id and verification
time. A new bundle revision requires new proof. Failed starts remain visible and
do not cause unbounded automatic retry sessions.

Windows hosts with the existing `run-session-host.ps1` supervisor wrapper can
register user-scoped autostart without requesting elevation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/windows-host-autostart.ps1 `
  -Runner "$env:LOCALAPPDATA\agent-hub\run-session-host.ps1"
```

The task uses S4U (no stored password, no interactive desktop), limited privileges,
a persistent one-minute time trigger, `StartWhenAvailable`, battery support and
`IgnoreNew`. It avoids BootTrigger, whose registration requires administrator
membership. A Windows policy denying S4U/batch logon is an explicit failure; the
installer never elevates or substitutes a logon-only task. A Linux fixture cannot
prove Windows scheduler execution: acceptance still requires a real reboot/wake
cycle and resumed checkin within two minutes on Tabo.
