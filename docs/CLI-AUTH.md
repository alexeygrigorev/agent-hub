# Hub CLI: install + human device auth

Self-contained Developer CLI. No private GitHub keys, no Hetzner-local paths,
no privileged install. Python >= 3.9 stdlib only.

## One-line install

```sh
curl -fsSL https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/cli/install.sh | sh
```

- Installs user-local `$HOME/.local/bin/hub` (override with `HUB_INSTALL_DIR`
  for tests; must stay under `$HOME`, never global, never overwrites `aplexer`).
- Verifies `python3 >= 3.9`, checks the `hub.pyz.sha256` manifest, fails
  atomically, prints `PATH` fixup when needed.
- Base URL override: `HUB_BASE_URL=https://... sh` (default is baked to the
  production Worker above).

## Human device login

```sh
hub auth login [--url https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev] [--label my-box]
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
  handled gracefully.
- `HUB_CONFIG_FILE` (absolute path) overrides both profiles into one file;
  human commands then check `client_kind == "human"` so a host token is never
  mistaken for human.

## Profiles

- Human: `~/.config/agent-hub/cli.json` (`client_kind: "human"`).
- Legacy host/agent: `~/.config/agent-hub/host.json` via existing
  `hub login URL --kind host|agent` (untouched).
- Normal user commands (sessions/intake/status/bus) use the human profile
  when present (fresh Linux after login); host enrollment/poll/agent-run
  stay host-only.

## Server contract (server worker implements; client expects)

- `POST /api/device/code` with `{kind: "human", label}` → `{device_code, user_code,
  verification_uri*, expires_in, interval}`. Identity, email, actor and scopes
  are never taken from the request; the approving allowlisted admin session
  binds them (human grants are `admin`) at approval time.
- `POST /api/device/token` with device grant → bearer + refresh pair.
- `GET /api/auth/me` → safe human identity/status (no tokens).
- `POST /api/auth/logout {}` → revokes ONLY the human credential family.

Today's Worker accepts the `human` kind, serves the two
`/api/auth/*` routes, and allowlists the anonymous `/cli/*` assets
(`install.sh`, `hub.pyz`, `hub.pyz.sha256`, `manifest.json`). Client filenames
are coordinated exactly.

## Build (head before deploy)

```sh
python3 scripts/build-hub-cli.py --out-dir public/cli
```

Writes deterministic stdlib zipapp `hub.pyz` (bundles `agent_hub_host` +
vendored `dtc_device_login` loop, pinned commit `c8ffec3`) + `.sha256` +
`manifest.json`. `install.sh` is tracked source; the three build outputs are
gitignored and rebuilt before every `wrangler deploy` so `./public` publishes
`/cli/*`.

## Limits

- No production auth/login/API/deploy calls from workers; mock-flow tests only.
- Session start/send/capture/stop UX is a separate followup (workspace is a
  logical label, `--brief-file` only); this doc does not advertise it.
