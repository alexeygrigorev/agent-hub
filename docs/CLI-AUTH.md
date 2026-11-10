# Hub CLI: install + human device auth

Self-contained Developer CLI. No private GitHub keys, no Hetzner-local paths,
no privileged install. Python >= 3.9 stdlib only.

The default Hub URL is `https://hub.dtcdev.click`. The former
`agent-hub.cloudflare-ai-agent-de9ca0.workers.dev` endpoint is a retired
bootstrap stub and must not be used for CLI or MCP traffic.

## One-line install

```sh
curl -fsSL https://hub.dtcdev.click/cli/install.sh | sh
```

- Installs user-local `$HOME/.local/bin/hub` (override with `HUB_INSTALL_DIR`
  for tests; must stay under `$HOME`, never global, never overwrites `aplexer`).
- Verifies `python3 >= 3.9`, checks the `hub.pyz.sha256` manifest, fails
  atomically, prints `PATH` fixup when needed.
- Base URL override: `HUB_BASE_URL=https://... sh` (default is
  `https://hub.dtcdev.click`).

## Update

```sh
hub update [--check] [--force]
```

- Checks the server's published CLI manifest (`/cli/manifest.json`), compares the
  build commit, verifies the SHA256 checksum, and atomically replaces the local binary.
- Use `--check` to detect whether the local CLI is stale without downloading.

## Human device login

```sh
hub auth login [--url https://hub.dtcdev.click] [--label my-box]
hub auth status [--json]
hub auth logout
```

- `login` uses the SAME Google/device mechanism as DataOps/dapier/shared auth:
  prints a verification URL + user code; approve in a browser signed in as the
  allowlisted Google account. Stores tokens in the human profile only:
  `~/.config/agent-hub/cli.json` (0600 file, 0700 dir, atomic write,
  survives reboot, refreshes via `/api/token/refresh` rotation).
- `status` shows safe identity only (never tokens). `logout` revokes ONLY the
  human credential family (`POST /api/auth/logout {}`) and removes ONLY the
  human config; the legacy host profile is preserved. Already-logged-out is
  reported as `Never logged in as human` with the login command. If Hub rejects
  the saved refresh token, status reports `Revoked by server (refresh replay or
  admin)` with the recovery command. Both unauthenticated states exit nonzero,
  including `--json`, so scripts can detect them.
- `HUB_CONFIG_FILE` (absolute path) overrides both profiles into one file;
  human commands then check `client_kind == "human"` so a host token is never
  mistaken for human.

### Migrating a stale workers.dev profile

If `cli.json` still stores the retired workers.dev URL, human login without an
override resolves that exact legacy origin to the Hub2 default. To migrate
explicitly, run:

```sh
hub auth login --url https://hub.dtcdev.click
```

The CLI obtains a new device grant from the selected Hub. It replaces the old
profile only after the Hub returns valid tokens, and does not carry old tokens
or server-bound profile metadata across origins. A cancelled or failed login
leaves the saved profile unchanged. Requests that still reach the retired
endpoint report `503 release_bootstrap` with the replacement URL and login
command.

Hub MCP clients, including Grok bots, should use
`https://hub.dtcdev.click/mcp` as their endpoint.

## Profiles

- Human: `~/.config/agent-hub/cli.json` (`client_kind: "human"`).
- Legacy host/agent: `~/.config/agent-hub/host.json` via existing
  `hub login URL --kind host|agent` (untouched).
- Normal user commands (sessions/intake/status/bus) use the human profile
  when present (fresh Linux after login); host enrollment/poll/agent-run
  stay host-only.
- Host enrollment provisions only its host credential in `host.json`. It does
  not create human CLI or service profiles and does not run `hub auth login`.
  A human CLI profile remains an explicit, separate choice. Optional loop
  service profiles, when needed, each require their own human approval and
  must use a dedicated `HUB_CONFIG_FILE`; they are not part of machine setup.

## Server contract (server worker implements; client expects)

- `POST /api/device/code` with `{kind: "human", label}` → `{device_code, user_code,
  verification_uri*, expires_in, interval}`. Identity, email, actor and scopes
  are never taken from the request; the approving allowlisted admin session
  binds them (human grants are `admin`) at approval time.
- `POST /api/device/token` with device grant → bearer + refresh pair.
- `GET /api/auth/me` → safe human identity/status (no tokens).
- `POST /api/auth/logout {}` → revokes ONLY the human credential family.

The Hub2 service accepts the `human` kind, serves the human auth routes, and
publishes the anonymous `/cli/*` assets (`install.sh`, `hub.pyz`,
`hub.pyz.sha256`, `manifest.json`). Client filenames are coordinated exactly.

## Build (head before deploy)

```sh
python3 scripts/build-hub-cli.py --out-dir public/cli
```

Writes deterministic stdlib zipapp `hub.pyz` (bundles `agent_hub_host` +
vendored `dtc_device_login` loop, pinned commit `c8ffec3`) + `.sha256` +
`manifest.json`. `install.sh` is tracked source; the VM installer builds these
assets into `hub2/public/cli` before the Hub2 service restarts.

## Limits

- Tests use mocked auth flows; the installed CLI talks to the configured Hub.
- Session start/send/capture/stop UX is a separate followup (workspace is a
  logical label, `--brief-file` only); this doc does not advertise it.
