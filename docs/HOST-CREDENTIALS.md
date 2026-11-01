# Host credentials: gh token, shared git SSH key, commit identity (intake 54d5b703)

Hub owns three non-engine credential kinds in `host_credentials` (AES-GCM at rest, numbered generation,
code in `src/host-credentials.ts`). Hosts materialize them from `host_credentials.py` on the machine
heartbeat (`provisioning.tick`). Nothing is copied between machines by hand and nothing is printed.

| kind | host effect |
| --- | --- |
| `gh_token` | `gh auth login --hostname github.com --with-token` (token on stdin), `gh auth setup-git`, `git_protocol ssh` |
| `git_ssh_key` | `~/.ssh/agent_hub_git_ed25519(.pub)` (0600; Windows: `icacls /inheritance:r`, no admin), one marked block at the top of `~/.ssh/config`, pinned GitHub host keys appended to `known_hosts` |
| `git_identity` | `git config --global user.name/user.email` when different |

Existing keys, config text and known_hosts lines are never removed: the key is a new file, the config
change is one `# BEGIN/END agent-hub git key` block replaced in place.

## Flow

* Operator (admin) writes: `hub host-credentials set-gh-token --from-gh | --token-file F | --stdin`,
  `generate-ssh-key [--rotate]`, `set-identity --name N --email E`, `register-key`, `resync`, `status`.
  Hub verifies a gh token against `api.github.com/user` and stores account + scopes as metadata.
* Hosts poll `GET /api/host-credentials/sync?have=kind:gen,...` about every 2 minutes. Only changed kinds
  (or everything after a re-sync request) return, so steady state carries no secrets and writes no rows.
* Hosts report `POST /api/host-credentials/status` (gh logged_in/account/scopes, `ssh -T` result, key
  fingerprint, applied generations; bounded slugs only), on change plus every 5 minutes.
* **AC4:** a report with gh logged out (or ssh denied) while Hub holds the kind makes Hub flag a re-sync for
  that machine; its next sync returns every credential and the host re-applies it. No agent ever asks a human.
* `hub host-credentials status` / `GET /api/host-credentials` show kinds (generation, account, scopes,
  fingerprint, registered) and per machine gh/ssh state, `stale` when no report for 15 minutes.

## Registering the public key (AC7)

`register-key` has Hub call `POST /user/keys` with the stored gh token. GitHub answers 404 when the token
lacks `admin:public_key`/`write:public_key`; Hub reports `missing_scope_admin_public_key`. Adding a scope to
a token needs a browser login, which agents must not start, so the token Hub holds must be minted with that
scope. An already-registered key (422 "already in use") counts as registered.

## Rules

Never read `~/.config/gh/hosts.yml`, never start `gh auth login` interactively or a browser flow, never put a
token in argv, logs or notes, never delete existing keys. Tests use a scratch `GH_CONFIG_DIR` and
`AGENT_HUB_SSH_DIR`; a failed apply keeps the old generation so the next poll retries.
