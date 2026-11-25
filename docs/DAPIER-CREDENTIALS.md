# Managed Dapier CLI credentials

Hub sessions use the Hub-owned `~/.config/agent-hub/shared/dapier/session.json`
as `orin@datatalks.club`. They never import the operator's
`~/.config/dapier/session.json`. Native Hub2 launches prepend a Hub-owned
`shared/bin/dapier` wrapper to PATH; managed engine profiles also link their
private Dapier config to the shared store. The wrapper sets XDG_CONFIG_HOME
only for Dapier and rejects absent, expired, non-device or wrong-identity
credentials. It does not change other CLIs' config directories.

## Initial pairing and recovery

Developer owns pairing through the authorized operator browser. On the target
host, run the real installed CLI with the shared XDG root:

```sh
XDG_CONFIG_HOME="$HOME/.config/agent-hub/shared" "$HOME/.local/bin/dapier" auth login
```

Use the command's private device code at
`https://dapier.dtcdev.click/device` (the CLI may open `/device?code=...`).
Verify that the approval page is signed in as `orin@datatalks.club` before
approving. Never approve as Alexey or copy the host session into this store.
Do not put the code, session token or credential file in logs, messages or Git.
If the operator browser is unavailable, report the credential dependency to
Developer; do not request an Alexey host login or route to NEEDS_ALEXEY.
The CLI writes its session atomically with owner-only permissions.

Check metadata without printing credentials:

```sh
XDG_CONFIG_HOME="$HOME/.config/agent-hub/shared" "$HOME/.local/bin/dapier" auth status
```

## Renewal and failure delivery

The Hub2 host heartbeat starts a bounded background maintenance worker. A
process lock serializes checks across host instances; a persisted hourly
checkpoint prevents repeated requests. With seven days remaining, the worker
calls Dapier's fixed HTTPS `/api/agent/device/refresh` endpoint and atomically
replaces the shared credential. Redirects are rejected. Already-running
sessions keep reading the same shared store after rotation.

The operator's Hub CLI profile (`~/.config/agent-hub/cli.json`) delivers
sanitized errors to Developer's registered `agent:dev` mailbox. The machine
credential is never used for this operator action. Each failure episode has
an idempotency key; failed alert deliveries retry on later hourly checks.
The daemon also logs an ERROR with a fixed reason, and records metadata in
`shared/dapier-health.json`. Remote response bodies and credential values
are never included. Success clears the episode so a later failure alerts
again. Missing pairing, wrong identity, refresh rejection (including 401),
invalid refresh responses and storage failures all produce errors.

An expired/revoked device session cannot be renewed: Developer repeats the
shared-store pairing above. A valid device session is rotated before expiry.
Keep the host daemon and its operator Hub profile healthy, including while no
sessions are running. POSIX hosts support this store; Windows is unchanged.

## Acceptance proof

After the exact reviewed release is deployed, start a real Hub session on the
target host and run `dapier auth status`. Verify the output says
`orin@datatalks.club` and `valid`, and that `command -v dapier` selects the
Hub shared wrapper. Separately inspect only the host login's expiry metadata
or establish that its file is absent. Do not remove or replace an operator
login to simulate expiry. Unit tests cover isolated temporary host and shared
stores; they do not substitute for this live acceptance proof.
