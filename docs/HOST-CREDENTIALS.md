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

**Precedence:** the managed block sits first in `~/.ssh/config` and carries `IdentitiesOnly yes`, so for
`github.com` the shared agent-hub key is the only identity used on that host — a personal GitHub key in
the same user's config is overridden while the block stands (other hosts in the config are untouched).
This is deliberate: all machines commit and push as the one shared key (AC6/AC8). Removing the marked
block by hand restores the previous behavior on that machine; the next sync re-adds it.

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

Until GitHub accepts the key, hosts hold it back: a sync payload for `git_ssh_key` carries
`registered: false`, the host skips installing it and records the generation as
`awaiting_github_registration`, so an unregistered key never overrides a working `github.com` ssh
identity. A ssh failure report does not schedule re-sync churn while the key is unregistered (nothing
would change). The moment `register-key` succeeds, Hub flags every machine for one forced sync, which
delivers the now-accepted key despite the unchanged generation.

## Rotation and revocation

`generate-ssh-key --rotate` stores generation N+1; every host overwrites the same key file
(`agent_hub_git_ed25519`) on its next poll, so there is no stale-key cleanup per host. Two things do NOT
happen automatically: the old public key stays registered on the GitHub account (remove it there by hand
or with `gh api -X DELETE user/keys/<id>` if it must stop authenticating), and a host that was already
synced keeps its copy of the old material even after it is revoked in Hub. **Revoking a host therefore
means rotating the gh token and the shared key**, which cuts off every machine until they re-sync — that
is the intended kill switch. `set-gh-token` bumps the gh_token generation the same way; Hub never deletes
a stored kind (replace it instead).

## Security notes (accepted risks)

* Every approved host token receives the plaintext token and private key — that is the design: Hub is the
  SSOT, each machine must materialize it. On the host, `gh`'s stored login and the key file are readable
  by any process running as the same OS user, including agent sessions on that host.
* At rest in Hub the payloads are AES-GCM sealed (HKDF from `SESSION_SECRET`, AAD `kind:generation`);
  admin responses carry only metadata, and status/list output is checked by test to never contain
  plaintext or ciphertext.
* Keep the gh token scope minimal: `repo` (or the repo-scoped fine-grained equivalent) for git work plus
  `admin:public_key`/`write:public_key` only because `register-key` needs it; anything wider is a leak
  waiting for a compromised host.

## Rules

Never read `~/.config/gh/hosts.yml`, never start `gh auth login` interactively or a browser flow, never put a
token in argv, logs or notes, never delete existing keys. Tests use a scratch `GH_CONFIG_DIR` and
`AGENT_HUB_SSH_DIR`; a failed apply keeps the old generation so the next poll retries.
