# Add an engine account once

The Hub stores managed engine accounts centrally. On the next heartbeat (normally
60 seconds), every enrolled machine running the current Hub host component installs
the pinned engine and a native `hub-<label>` aplexer profile. New machines receive
the same accounts. Removing an account withdraws its Hub routes immediately and
removes its profiles, engine files and delivered credentials on each machine's next
successful heartbeat. Offline machines reconcile when they reconnect.

## Machine prerequisites and installation

Linux machines need Python 3.9+, Node.js 22+ with npm, and a working `aplexer` binary
on PATH. Install the host under the same OS user that runs aplexer:

```bash
curl -fsSL https://hub.dtcdev.click/cli/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
hub login https://hub.dtcdev.click --kind host
# Approve the displayed pairing once in the Hub browser UI.
hub run
```

Keep `hub run` running with the machine's existing service manager. There is no
new timer or service installed by account addition. Existing hosts must upgrade
their Hub component (`hub update`) and restart their Hub host process once to load
this feature. Source-package installations can instead use the existing documented
virtual environment installation in [DEPLOY.md](DEPLOY.md).

The host daemon syncs at startup and each heartbeat. `hub engines sync` forces an
immediate sync using the host credential, even when a human CLI profile is also
logged in. Machine enrollment authorizes that machine to receive **all** managed
bundles: enroll only trusted machines. Revoking a machine stops subsequent secret
delivery; it cannot erase credentials from an offline machine or recall credentials
already held by running processes. Revoke provider credentials separately if needed.

## Add or update an account

Authenticate the operator CLI once with `hub auth login`. Import an existing
native account in one command (its auth file must be private):

```bash
hub engines add --provider codex --label work-codex --auth-file "$HOME/.codex/auth.json"
```

Use `--config-file` to include the native config, or `--version` to select another
exact engine version. `go` and `zai` accept the same native-file import (`zai` also accepts `--token-file`); Claude uses `--token-file` and Gemini uses machine login as described below:

```bash
hub engines add --provider zai --label my-zai --token-file /private/zai-token
hub engines add --provider zai --label work-zai --auth-file "$HOME/.zcode/v2/credentials.json"
```
For custom environment authentication or several config files, prepare a private JSON
file in a directory with mode 0700; the file must be mode 0600. For a Codex account,
the bundle has this shape (replace the synthetic value locally):

```json
{
  "label": "work-codex",
  "provider": "codex",
  "version": "0.163.0",
  "files": {".codex/auth.json": "<contents of your account's auth.json>"},
  "env": {},
  "args": []
}
```

A local Python command can build the private bundle without displaying credentials:

```bash
mkdir -p -m 700 "$HOME/.config/agent-hub/import"
python3 - <<'PY'
import json, os
from pathlib import Path
account = Path.home() / '.codex/auth.json'
target = Path.home() / '.config/agent-hub/import/work-codex.json'
payload = {'label': 'work-codex', 'provider': 'codex', 'version': '0.163.0',
           'files': {'.codex/auth.json': account.read_text()}, 'env': {}, 'args': []}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as out:
    json.dump(payload, out)
PY
hub engines add --file "$HOME/.config/agent-hub/import/work-codex.json"
```

The add command is the only account-add operation. Re-running it rotates the entire bundle
and causes each machine to replace its installation. `hub engines list` shows
public metadata and acknowledged installation revisions, never credentials.

Supported npm engines: `codex` (`@openai/codex`), `claude`
(`@anthropic-ai/claude-code`), `gemini` (`@google/gemini-cli`), and `go`
(`opencode-ai`). Versions must be exact semver; arbitrary packages and install
scripts are refused. Provider config and auth files belong in `files`, with paths
relative to the account's isolated HOME. Environment authentication is also supported
via the bounded provider-specific names in `env`; do not put secrets in `args`.
OpenCode `args` must be exactly
`["--model", "opencode-go/muse-spark-1.3-contributor"]` (an optional final `--version` is accepted for diagnostic-only starts).

Z.ai is centrally managed using the native `zcodex` engine (`alexeygrigorev/codex-zcode`).
On hosts with the native binary and `/opt/ZCode/resources/glm/zcode.cjs` runtime,
an isolated executable wrapper and account environment (`CODEX_HOME` and `ZCODE_HOME`)
are installed under `hub-<label>`. Clean hosts without pre-installed zcodex automatically
fetch the exact pinned release artifact. If the required ZCode JS runtime is missing,
installation fails closed and holds until runtime prerequisites are satisfied.
Multiple Z.ai labels on one machine coexist safely with independent credential stores.
Windows provisioning has not been accepted; the Docker acceptance test covers Linux only.

Once installed, a local session uses `aplexer start --profile hub-work-codex`.
Existing Hub interactive placement uses the registered engine/account/profile
route and the configured workspace mapping. The server quota collector discovers managed accounts with `hub engines quota`,
which probes each isolated account and returns only sanitized metadata. Quota and resource admission still
apply; installation does not prove model access or grant quota. Named-profile
headless execution retains the existing launcher's hold until that adapter is
qualified. This feature does not bypass that hold.

## Remove

```bash
hub engines remove work-codex
hub engines list
```

Only managed account data is removed. Existing native profiles outside the
`# BEGIN HUB MANAGED ENGINES` block, unrelated sessions and local accounts are
preserved. A managed-name collision fails closed. Removal does not terminate
already-running sessions.

## Credential handling

Bundles use AES-256-GCM with random nonces and label/revision authenticated data.
The key is derived with HKDF-SHA256 from the existing Worker `SESSION_SECRET`,
with a separate provisioning context. SQLite stores ciphertext, never plaintext
bundles. Rotating `SESSION_SECRET` requires re-adding the managed bundles; retain
the old key securely for recovery until replacement is complete.

Only current approved host credential families can fetch decrypted bundles over
the existing HTTPS transport. Human listing and fleet/UI data contain no bundles.
Host files use no-follow checks, owner-only directories and atomic mode-0600 writes.
Engine installs run without credentials or install scripts. Native profile commands
contain only public labels and the wrapper path; credentials enter only the engine
child environment or its isolated HOME. No provider stdout/stderr is copied into
installation errors. Engine credentials never enter aplexer profile TOML.

## Automated Docker acceptance

On Linux with Docker, npm dependencies installed, and a real compatible aplexer
binary on PATH:

```bash
npm ci
scripts/test-provisioning-docker.sh
```

`APLEXER_BINARY=/absolute/path/to/aplexer` selects an alternate binary. The script
runs a local-only Worker fixture on exclusive loopback port 19487 and a clean,
unprivileged Node/Python container with 1500 MiB memory and 256 tasks. The only
copied host executable is aplexer; no host HOME, credentials or config is mounted.
The container installs Hub via `/cli/install.sh`, uses real device enrollment and
host registration, then keeps the installed machine daemon running. It centrally
adds an engine, waits for automatic installation, starts a real aplexer session
executing the installed Codex `--version`, removes the bundle, waits for automatic
withdrawal and verifies credential deletion and start refusal. This proves native
installation/start/removal without making a model request or spending credits.
It does not claim a paid model inference completed. All credentials are synthetic.
The script also verifies actual Claude, OpenCode and Gemini version sessions, including direct native-package binary selection with npm scripts disabled. The test fixture is separate from the production Worker entrypoint.

## Provider authentication findings (20 October 2026)

This feature is for the same subscription owner using their own trusted machines.
It does not lend an account to another person or pool accounts to bypass limits.

- **Codex:** [official authentication docs](https://learn.chatgpt.com/docs/auth#fallback-authenticate-locally-and-copy-your-auth-cache)
  explicitly document copying the auth cache to a headless machine, including
  Docker. Use the native private-file import. Device authentication is preferred
  for a fresh login but is not stated as mandatory per machine.
- **Claude Code:** [official authentication docs](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
  document `claude setup-token` for subscription-backed scripts/CI. Use that
  portable token via a private file and `--token-file`; the convenience import
  does not copy the refreshable `.credentials.json` cache.
  [Consumer terms](https://www.anthropic.com/legal/consumer-terms) prohibit making
  credentials or the account available to another person. Same-owner native CLI
  automation uses the documented token mechanism; no third-party inference client
  is substituted.
- **Gemini:** [official authentication docs](https://geminicli.com/docs/get-started/authentication/)
  document Google sign-in and a local cache, and separately support API keys.
  [Terms and privacy guidance](https://geminicli.com/docs/resources/tos-privacy/)
  require supported clients for Google subscription access. No express cache-copy
  permission or separate-login mandate was found. The conservative choice here
  is native login on each machine, with a Hub consent relay. No Google OAuth
  cache is distributed and no subscription is converted to API billing. The
  [official CLI source](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/code_assist/oauth2.ts)
  implements a manual browser-link/authorization-code flow; this is not a
  device-code protocol and the UI labels it accurately.
- **OpenCode Go:** [official Go docs](https://opencode.ai/v2/docs/console/go/)
  use an API key and restrict Go workspaces to one member.
  [Terms](https://opencode.ai/legal/terms-of-service) restrict use to the owner's
  internal use and prohibit multi-account circumvention. An existing owner's Go
  key can be delivered to their native OpenCode clients; no purchase, extra
  member or separate device subscription is created. This is an inference from
  the documented portable-key mechanism, not an express multi-device guarantee.
- **Z.ai/ZCode:** [official configuration docs](https://zcode.z.ai/en/docs/configuration)
  describe Coding Plan account login and API-key connections. Central accounts
  are distributed to eligible hosts running the native zcodex/ZCode runtime wrapper.
  Importing via `--token-file` or `--auth-file` creates isolated native configuration
  and `quse zai` quota monitoring without synthesizing OAuth/JWT tokens. Several
  labels on one host coexist as distinct `hub-<label>` profiles with isolated
  `CODEX_HOME` and `ZCODE_HOME`. GLM models route strictly through `zcodex` and are
  refused on OpenCode.

For Gemini, run `hub engines add --provider gemini --label my-google`, then open
[Engine accounts](https://hub.dtcdev.click/engines.html). Choose **Sign in with
Google** for each installed machine, open the native provider link, complete
consent and submit the returned authorization code. Hub encrypts that short-lived
code for only the target machine; the host's native CLI exchanges it. Provider
tokens stay on that machine. The login PTY has no disk transcript and never receives
a model prompt. Successful login enables its route on the next heartbeat.

## Hub owns every login (intake 5814e109)

Hub is the single source of truth for each managed account's credential. The
imported bundle is only the install snapshot; the current credential lives in
`engine_credentials` (AES-GCM, same key derivation) as a numbered **generation**.

- **Central refresh.** The five-minute cron refreshes OAuth tokens in Hub before
  any engine would: Codex when `last_refresh` is three days old or the access
  token has under three days left (Codex itself waits eight days); Claude when
  under three hours of validity remain. Each refresh publishes a new generation.
  One owner per token family, so a refresh can never stale another host.
- **Hosts materialize, never own.** `hub engines sync` (each heartbeat) and every
  launch (`execute` fetches `GET /api/engines/<label>/credential`) write Hub's
  current generation into the isolated HOME. Only digests are kept locally
  (`credential-state.json`). A generation change never reinstalls the engine.
- **Write-back with CAS.** If an engine still rotated a token locally, the host
  posts it with `base_generation`; Hub accepts it when based on the current
  generation or provably newer (Codex `last_refresh`, Claude `expiresAt`) and
  every other host converges. A stale copy is refused and replaced.
- **Quota without quse.** Hub reads quota directly from the provider with the
  stored credential (ChatGPT `wham/usage`, Anthropic `oauth/usage`) into
  `server_quota_snapshots`. Host admission and launch ask Hub
  (`/api/engines/admission`); no host runs a quota CLI, and a host without
  `quse` is never rejected. The server collector's managed path reads the
  provider directly too (`credentials.probe_quota`).
- **Rejections.** A provider-rejected credential is marked `invalid`, alerts once
  (`engine_credential` alert) and is offered nowhere. A host that reports a
  generation rejected (`POST /api/engines/<label>/rejection`, or a launch failing
  `engine_auth_expired`) is not offered that account again until Hub publishes a
  newer generation. A fresh host-advertised route rejection is always honoured.
- **New hosts.** Enrolment plus one sync installs every account at its current
  generation; there is no per-host provider login.

Claude gets its own Hub-owned token family through a central login (never a copy
of a machine's `~/.claude`, whose rotation would invalidate the other copy):

```bash
hub engines login --provider claude --label hub-claude   # open the URL, paste the code (not echoed)
hub engines list        # credentials[]: generation, status, expiry - never secrets
hub engines maintain    # operator: run central refresh + direct quota now
```

Gemini accounts may import their OAuth cache (`--provider gemini --auth-file
~/.gemini/oauth_creds.json`); Google refresh tokens do not rotate. OpenCode Go
keeps host-local device login (see OPENCODE-AUTH.md). Bundles imported before
this change are adopted as generation 1 on the first cron pass, with a two-hour
grace so a host holding the newer rotated copy writes it back before Hub
refreshes or judges the old snapshot.

The native-login relay is Linux-only and has bounded five-minute login processes.
Its protocol and UI are tested with synthetic consent; no real Google consent is
claimed. Existing per-provider credential expiry or provider revocation can require
renewal. Codex and Claude refresh-token rotation is centralized (see above).

## Canonical managed-engine root (intake 59a6f44a AC1)

One source of truth: `host/agent_hub_host/provisioning.py::root()`.

- Default: `~/.config/agent-hub/managed-engines` (`config.get_host_config_path().parent / 'managed-engines'`).
- Override: `AGENT_HUB_MANAGED_DIR` when explicitly set.
- Interactive dispatch (`host/agent_hub_host/fleet.py` `provisioning.module_command('execute', ...)` embeds `AGENT_HUB_MANAGED_DIR=<root>`) and launcher headless starts (`fleet.py` sends `payload["managed_dir"] = str(provisioning.root())`, launcher `fetch_managed_quse` falls back to `AGENT_HUB_MANAGED_DIR` then `~/.config/agent-hub/managed-engines`) use the same root per account. Refresh-token rotation in one copy must never break another because there is only one live copy per host; `~/git/agent-hub/.local/placement-recovery/managed-engines` is a legacy recovery snapshot and is not used by the active host (`agent-hub-session-host.service` sets `AGENT_HUB_MANAGED_DIR=/home/alexey/.config/agent-hub/managed-engines`).
- Same-revision reconcile and execution preserve nonempty runtime files after validating their private parent, owner, regular-file type and single-link confinement; POSIX file modes are tightened to 0600. Missing or truncated files are restored from the bundle. This preserves refresh-token rotation instead of resetting credentials from an older central snapshot.
- To heal an invalid credential, re-import it (`hub engines add --provider codex --label <account> --auth-file <private-authorized-source>`) or, for Claude, `hub engines login`; Hub publishes a new generation and every host converges on its next sync or launch. Never hand-copy secrets or print tokens. Verify with `hub engines list` (credential `status: ok`) and `hub quota` (fresh `hub-direct` reading).

## Multiple profiles and the same launch everywhere

Each managed label is an account, with its own HOME, credential files, aplexer
profile and shared Hub concurrency/quota pool. Add two Codex accounts under two
labels; no default HOME or default account alias is used by either wrapper.

```bash
hub engines add --provider codex --label personal --auth-file "$HOME/.codex/auth.json"
hub engines add --provider codex --label work --auth-file "$HOME/.codex-work/auth.json"
hub start-session --machine MACHINE_A --workspace home --account personal
hub start-session --machine MACHINE_B --workspace home --account work
```

The remote start commands use the existing Fleet host consumer and its trusted
workspace mapping. Account pinning prevents selection of the other account.
Every host uses the central engine/version/arguments definition through its
`hub-<label>` profile. The Docker test exercises the same managed wrapper/profile,
with `--version` as its central diagnostic arguments and no model call. This does
not qualify named-profile headless adapters, or install a second Fleet consumer.

For Claude subscription setup, run `claude setup-token` once with its native
browser consent, save the printed token privately without putting it in shell
history, then:

```bash
hub engines add --provider claude --label my-claude --token-file /private/token-file
```

Browser consent relay check (requires Playwright available through
`PLAYWRIGHT_PACKAGE`, defaulting to the existing local tooling):

```bash
node test/provisioning-ui.browser.mjs
```

The test uses a real local Worker and SQLite with a synthetic native consent link.
It covers machine login requests, the Google link, private code submission and the
resulting state, and writes a screenshot under gitignored `.local/ui-evidence/`.
