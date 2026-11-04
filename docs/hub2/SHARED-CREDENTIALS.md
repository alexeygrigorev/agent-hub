# Hub-shared host credentials + shared-config bundle (54d5b703, 0043dadb)

Status: implementation checkpoint for the Hub2 credential canary (worktree
`agent-hub-wt-hub2-cred-1104`, base hub-2.0 @ 09bde6ca). Implemented by
`hub2-canary-cred` (Alexey-authorized direct launch, Hub API down). Not
deployed; the Hub2 coordinator integrates. **Target-corrected (Alexey,
2026-11-04): the central authority lives in `hub2/`, NOT in the superseded
Hub1 `src/` server — an earlier src/ duplicate was removed, not kept.**

## What was built

GitHub CLI auth (`gh`) and one shared additive git SSH key become Hub-shared
**credential kinds**, and a versioned non-secret **shared-config bundle**
(first entry: AWS Gate) is distributed to every enrolled machine — following
the hub2/ central patterns (`ControlDO` + `MeteredDb` + `credential-crypto`
sealing), kept strictly isolated from operator/human/worker credentials and
from per-session worker bindings.

### Worker — central authority in `hub2/` (coordinator-owned scope)

- `hub2/src/shared-credentials.ts` — `SharedCredentials` sub-service inside the
  ControlDO (dispatched like `WorkerCredentials`/`ControlAuth`), own tables via
  the ControlDO schema **v2 migration** (`SHARED_CREDENTIAL_MIGRATIONS`):
  `shared_credentials`, `shared_credential_acks`, `shared_credential_rejections`
  (all `WITHOUT ROWID`). Sealed at rest with `HUB2_CREDENTIAL_KEY` and AAD
  `["hub2-shared-credential", kind, generation]` (same key family as worker
  credentials; the AAD binds kind+generation so kinds cannot substitute).
  - `POST /api/shared-credentials/<kind>` (`operator:admin`): publish the next
    generation. Exact per-kind file allowlist: `gh` → `gh/hosts.yml`; `git-ssh`
    → `git-ssh/id_ed25519` + `git-ssh/id_ed25519.pub`. Structural validation
    (gh https login for github.com; complete OpenSSH ed25519 pair). Optional
    CAS `base_generation` → 409 `generation_changed` naming the current one.
    Content is never echoed back.
  - `GET /api/shared-credentials` (machine actor with the pairing-granted
    `machine:heartbeat` scope — no re-pairing needed): current generations +
    files for materialization.
  - `POST /api/shared-credentials/<kind>/ack` (machine): acknowledge a
    generation; gh acks carry a bounded non-secret `gh_auth` report
    (installed, logged_in, account, scopes, checked_at) → AC3 visibility.
  - `POST /api/shared-credentials/<kind>/rejection` (machine): AC4 report
    (`gh_auth_missing`); a newer published generation clears rejections.
  - `GET /api/shared-credentials/status` (`operator:admin`): non-secret
    metadata, per-machine acks + gh auth, rejections.
  - `POST /api/shared-credentials/git-ssh/register-github` (`operator:admin`):
    AC7 — registers the stored public key on GitHub via the stored gh token
    (`GET/POST https://api.github.com/user/keys`), idempotent by key blob;
    fixed reason codes only, tokens/keys never in responses.
- `hub2/src/control-do.ts` — schema version 1→2 with the migration list;
  `SharedCredentials` constructed + dispatched after `WorkerCredentials`.
- `hub2/src/worker.ts` — `/api/shared-credentials*` forwarded to the CONTROL DO
  like the other control routes.
- No Hub1 `src/` authority was kept: the earlier `src/shared-credentials.ts` +
  `src/hub.ts` wiring + Hub1-harness test were removed (correction: do not
  duplicate retired Hub1 authority).

### Host — maintained pattern reused; **shared-file edits PENDING host-owner ACK**

Existing maintained host pattern is reused. The following touch **shared host
files** and must not be integrated until the host owner ACKs them (request
relayed via the coordinator; see also `.local/pending-cred-dist.json`):

- `host/agent_hub_host/provisioning.py` — `sync()` runs both shared syncs
  non-fatally (startup + heartbeat = installer/updater path);
  `engine_environment()` sets `AWS_CONFIG_FILE` → Hub-owned
  `shared/aws/config` and `AWS_SHARED_CREDENTIALS_FILE` →
  `<managed home>/.aws/credentials` (never exists ⇒ per-machine static keys
  such as `main-temp` unreachable — 0043dadb AC4), and prefers the
  materialized `GH_CONFIG_DIR` / `GIT_CONFIG_GLOBAL` / `GIT_SSH_COMMAND`.
  Hosts without a materialized copy keep prior behavior unchanged.
- `host/agent_hub_host/fleet.py` — `aws_gate_credential_process` added to the
  `load_settings` key allowlist (non-secret string ≤ 2048 bytes).
- `host/agent_hub_host/cli.py` — `hub shared-credentials` subcommand
  registration.

New host modules (additions, same ACK request):

- `host/agent_hub_host/shared_config.py` — versioned non-secret bundle sync:
  repo `shared-config/<folder>/` → Hub-owned `<agent-hub config>/shared/<folder>/`;
  placeholder substitution per host; missing setting ⇒ folder **skipped
  fail-closed**; version digest in `shared/config-state.json`; `dry_run` for
  the install-on-new-machine path (0043dadb AC8).
- `host/agent_hub_host/shared_credentials.py` — materializes gh + git-ssh
  generations into `shared/gh/` and `shared/git-ssh/` (Hub is the only writer;
  no write-back), regenerates derived non-secret support files (gitconfig with
  `credential.helper = !gh auth git-credential`; `ssh_config` with User git,
  IdentityFile, IdentitiesOnly, pinned `UserKnownHostsFile`,
  StrictHostKeyChecking yes, BatchMode; `known_hosts` with GitHub's published
  host keys). `sync_shared(request)` = GET → materialize → ack (+ `gh_auth`);
  materialized-but-logged-out ⇒ `gh_auth_missing` rejection (AC4). Proof
  helpers: `gh_auth_status`, `gh_scratch_proof` (AC5 throwaway GH_CONFIG_DIR),
  `git_ssh_proof` (`ssh -T git@github.com`), `git_push_dry_run` (AC8).
- Host tests: `host/tests/test_shared_config_0043dadb.py` (5),
  `host/tests/test_shared_credentials_54d5b703.py` (8).
- Host request paths match the hub2/ public routes (`/api/shared-credentials*`);
  the host transport adapter (bridge/device_transport, host-owner domain)
  forwards them as machine-authenticated requests.

### Repo bundle

- `shared-config/aws/config` — profile `default` only, `credential_process`
  placeholder, no static keys. `shared-config/README.md` — layout, AWS Gate
  notes, how to add folders, install-on-new-machine, supported ops + limits.

## Isolation guarantees

- Kind namespaces are exact allowlists, enforced on publish (hub2/) and on
  materialize (host); a `gh` entry carrying a `git-ssh` path materializes
  nothing and advances no generation (tested on both sides).
- Separate tables, separate AAD (`hub2-shared-credential` + kind + generation),
  separate on-host directories; operator/human/worker credential surfaces and
  `hub2/src/worker-credentials.ts` untouched.
- The shared key is additive by construction: hosts write only inside the
  Hub-owned shared root; user `~/.ssh`, `~/.config/gh`, `~/.aws` are never read
  or written (tested with a sentinel key).
- Sealed bytes at rest never contain plaintext token material (tested); acks,
  status, CLI output and proofs carry fixed reason codes + account names only.

## Tests

- `hub2/tests/shared-credentials.test.ts` + `run-shared-credentials.mjs` (7):
  kind isolation, structural validation, publish/CAS/distribute/ack (bounded
  gh_auth; sealed-at-rest check; no token in status), rejection supersession,
  operator-only gates, GitHub registration idempotency + failure codes, schema
  v2 migration. **7/7 pass.**
- hub2 regression (unchanged suites after the ControlDO v2 bump):
  control 9/9, project-work 8/8, project-intake 5/5, read-cache 10/10,
  db-metering 4/4, accounts-mail 46/46. `npm --prefix hub2 run typecheck` clean.
- Host: pytest 13/13 (targeted files), run under `scripts/capped-test-run.sh`
  with disposable HOME. Superseded Hub1-harness vitest file was removed with
  the src/ duplicate.

## Limitations (coordinator attention)

1. **Host-owner ACK gate.** The `host/agent_hub_host/{provisioning,fleet,cli}.py`
   edits (and the two new host modules) must not be integrated before the host
   owner ACKs; the request is relayed through the coordinator. Workstream
   ledger ("identity"): host interfaces retain existing owners.
2. **No live multi-host proof.** AC5 (54d5b703: `gh auth status` OK on all 4
   machines after a logout, scratch GH_CONFIG_DIR) and AC8 (`ssh -T` + dry-run
   push per machine) plus 0043dadb AC7 (`aws sts get-caller-identity` from a
   Hub-launched session returning 817685572750, `main-temp` failing) are
   deploy-time proofs; helpers exist, nothing real was touched here.
3. **Windows paths.** Host materialization and env wiring are portable
   (`os.name` guards, `private_fs`/`win32_fs`); the Python proof harness is
   POSIX-marked; real Windows ACL verification outstanding. Hub writes the gh
   config directly (equivalent non-interactive to `gh auth login --with-token`,
   AC2).
4. **`gh` binary presence.** The https helper needs `gh` on PATH at session
   time; `gh_auth_status.installed=false` surfaces that to Hub via acks. The gh
   installer itself is out of scope.
5. **hub2 production-line budget.** The new module adds ~280 product SLOC to a
   snapshot that already failed the budget gate by 362 SLOC (FEATURES.md);
   coordinator owns the reconciliation.
6. **Workerd E2E.** New coverage runs in the node:sqlite ControlDO harness
   (like the control suite); workerd core/normal-day runners were not extended
   to drive these routes end-to-end.
7. **Key generation + first publish are operator/Hub actions**: generate the
   ed25519 keypair once (off-repo), `POST /api/shared-credentials/gh` and
   `/git-ssh`, then `POST /api/shared-credentials/git-ssh/register-github`.
   No key material was created or stored in this worktree.

## Integration notes

- Two commits on `agent-hub-wt-hub2-cred-1104`:
  1. hub2/ central authority + schema v2 + routes + tests + `shared-config/`
     bundle + this doc — integrates onto hub-2.0 at the coordinator's call.
  2. Host modules + shared-file edits — **HOLD for host-owner ACK**; do not
     cherry-pick until acknowledged. Branch was not pushed (Hub API down;
     direct-launch canary per HUB2-CRED-BRIEF.md).
- The Hub1 `src/` server stays untouched at its checkpoint state; the earlier
  src/-side duplicate of this authority was removed, not kept.
