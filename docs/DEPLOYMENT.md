# Agent Hub CI/CD Deployment & Rollback Guide

This guide documents the continuous integration, deployment, and manual rollback pipeline for the **Agent Hub** Cloudflare Worker (`alexeygrigorev/agent-hub`).

---

## 1. Pipeline Overview & Architecture

Agent Hub runs as a Cloudflare Worker (`agent-hub`) powered by:
- A Durable Object (`Hub`) managing state, machine registry, intake, and session coordination.
- Static assets served via the Workers Assets binding from `./public`, specifically including the Developer CLI bundle (`/cli/install.sh`, `/cli/hub.pyz`, `/cli/hub.pyz.sha256`, `/cli/manifest.json`).

Continuous deployment is orchestrated via GitHub Actions workflows:
- **`.github/workflows/deploy.yml`**: Main CI/CD pipeline running focused typechecks, tests, CLI packaging, and Worker deployment on push to `main` (and PR verification).
- **`.github/workflows/rollback.yml`**: Dedicated manual rollback workflow for instant, zero-rebuild traffic shifting to any prior Worker Version ID.
- **`scripts/ci/validate-workflow.py`**: Static linter ensuring workflow YAML, path filters, timeout limits, and secret safeguards pass with 0 external dependencies.
- **`scripts/ci/rollback.py`**: CLI rollback helper wrapping Wrangler version deployment commands.

---

## 1b. Release slots (orchestrator release gate, intake 2c87c231)

Every production deploy consumes exactly one **release slot**: a single-use, target-bound record the orchestrator issued through the Hub. Main-push CI checks the slot without consuming it. A real `workflow_dispatch` deploy checks and consumes it as its first deploy-job step (`scripts/ci/release_slot_verify.py`), before any build or deploy action. Either check refuses with the reason when the slot is missing, already consumed, revoked, expired, or bound to a different target. The gate fails closed: an unreachable Hub also refuses.

**Flow for a deploy-relevant push to `main`:**

1. Choose a random UUID v4 slot id before finalizing the commit and add `Release-Slot: <uuid>` as a commit trailer. Keep the existing `Scope: in` or `Scope: gray <intake-UUID>` trailer too.
2. With the final commit SHA known, send that SHA and UUID to the orchestrator (`aplexer message send --to orchestrator-main`), or run as the orchestrator: `hub releases issue --sha <full-sha> --id <uuid> [--ttl-hours N] [--note "..."]` (admin credential required; default TTL 48 h, max 336 h). The Hub rejects SHA slots without a UUID so a guessable capability cannot be issued.
3. Push `HEAD:main` in the orchestrator's release slot. The first CI job validates the trailer and SHA without consuming the record, then the test job runs. A docs-only push is outside the deployment path filters and needs no release slot.
4. To deploy that tested SHA, dispatch `deploy.yml` on `main`. The gate repeats the SHA/trailer check and consumes the slot atomically before any deploy step. A dry-run dispatch checks the slot without consuming it. A rollback dispatch similarly consumes a separate slot issued for the exact target version id.
5. If a check refuses, the Hub response says why: `release_slot_not_found` (never issued / typo), `release_slot_target_mismatch` (slot bound to another SHA), `release_slot_already_consumed` (single-use), `release_slot_revoked`, `release_slot_expired` (ask for a fresh one), or gate-unreachable (fail closed). Fix the cause and get a fresh slot. A refused push never deploys and a check-only failure never burns the slot.

**Rules and exemptions:**

- Deploy-relevant pushes (the `deploy.yml` path filter: `src/**`, `host/**`, `test/**`, `scripts/**`, `public/**`, configs, workflows) require a slot. Docs-only pushes and `[skip ci]` commits never reach the deploy job and are exempt by rule. Pull requests never deploy and need no slot.
- Manual `workflow_dispatch` runs of `deploy.yml` run the same gate step unconditionally — dispatching cannot bypass the gate, even with `skip_tests` or `dry_run`.
- `rollback.yml` requires a `release_slot` input consumed against the exact version id: `hub releases issue --target <version_id>`, then dispatch with that slot. Incident flow: read the version id (`npx wrangler versions list`), issue the version-bound slot, dispatch.
- Only one live (issued, unexpired) slot may exist per target; re-issue after refusal requires the old slot to be consumed, revoked (`hub releases revoke <id>`), or expired (superseded automatically on re-issue). Issue/show/list/revoke are admin-only; the verify endpoint is a capability endpoint — the random UUID v4 slot id is the credential, so no CI secret is needed.
- Slots and every issue/consume/revoke are audited in the Hub `audit` table (`release_slot_*` actions). Reads are indexed and bounded (`release_slots_active_target` partial unique index, list LIMIT ≤ 200); the public verify endpoint writes only on a successful consume.

---

## 2. Quota & Trigger Optimization (90% Quota Gate)

To strictly conserve GitHub Actions runner minutes and remain well within the 90% quota boundary:
1. **Path Filtering**:
   Workflows trigger only when relevant application source, scripts, configuration, or workflows change:
   - `src/**`
   - `host/**`
   - `public/**`
   - `scripts/**`
   - `wrangler.jsonc`
   - `package.json`, `package-lock.json`, `tsconfig.json`, `vitest.config.ts`
   - `.github/workflows/**`
2. **Docs Excluded from Deploys**:
   Changes to documentation (`docs/**`, `*.md`, `.local/**`) do **not** trigger test or deploy jobs.
3. **Commit Skip Directives**:
   Commits marked with `[skip ci]` or `[ci skip]` are respected natively by GitHub Actions and will not execute runner jobs.
4. **Focused Test Suites**:
   Rather than running an unbounded, slow full matrix, the CI pipeline runs bounded typechecking, focused Worker Vitest suites (`human-auth`, `token-auth`, `session-control`, `fleet`), CLI build verification, and critical Python regressions.

---

## 3. Secrets Management & Permissions

- **GitHub Repository Secrets**:
  - `CLOUDFLARE_API_TOKEN`: Cloudflare API token with Workers Scripts write permissions.
  - `CLOUDFLARE_ACCOUNT_ID`: Cloudflare Account ID for the target deployment.
- **Zero Secrets in Git**:
  Tokens, keys, and private URLs are never committed to the repository, emitted into build logs, or exposed in public documentation.
- **Minimal Workflow Permissions**:
  Workflows declare minimal permissions:
  ```yaml
  permissions:
    contents: read
  ```

---

## 4. Pre-Deploy Build Contract

The Hub CLI zipapp and metadata must be deterministically built from the exact checked-out source commit prior to running `wrangler deploy`:
```bash
python3 scripts/build-hub-cli.py --out-dir public/cli
```
This produces gitignored artifacts:
- `public/cli/hub.pyz`
- `public/cli/hub.pyz.sha256`
- `public/cli/manifest.json`

Because Wrangler uploads the contents of `./public` to Cloudflare Assets, running this build step before `wrangler deploy` guarantees that CLI assets served under `/cli/*` are never missing or out-of-date.

---

## 5. Worker Deployment Contract

The deployment step executes inside `.github/workflows/deploy.yml` on push to `main` (after tests succeed) or via `workflow_dispatch`:
```bash
SHORT_SHA=$(git rev-parse --short=12 HEAD)
FULL_SHA=$(git rev-parse HEAD)
npx wrangler deploy --tag "$SHORT_SHA" --message "source_commit=$FULL_SHA"
```

### Configuration & State Preservation
The deploy uses the root `wrangler.jsonc`:
- **Worker Name**: `agent-hub`
- **Durable Object Binding**: `env.HUB` (class `Hub`)
- **Assets Binding**: `env.ASSETS` pointing to `./public`
- **Environment Variables**:
  - `HUB_ADDITIONAL_ADMINS` (e.g. `orin@datatalks.club`)
  - `AUTH_CLIENT_ID`
  - `AUTH_ISSUER`
  - `AUTH_BASE_URL`
  - `AUTH_CALLBACK_URL`
  - `AUTH_JWKS_URL`

Durable Object storage, existing secrets, and configured variables remain intact across deployments.

---

## 6. Manual Rollback Procedures

Cloudflare Workers Versioning enables instant traffic shift to an exact prior Version ID without requiring Git revert commits, rebuilds, or force-pushes.

### Method A: GitHub Actions (Recommended)
1. Navigate to **Actions** -> **Manual Rollback** (`rollback.yml`).
2. Click **Run workflow**.
3. Enter the target `version_id` (e.g., `cf8aef89-43de-4732-a2aa-e34ce91a426d`).
4. Optionally enter a reason/message (e.g., "Rolling back due to incident #XYZ").
5. Click **Run workflow**. The workflow deploys the target version to 100% traffic immediately.

Or trigger via GitHub CLI:
```bash
gh workflow run rollback.yml -f version_id="<VERSION_ID>" -f message="Rollback to <VERSION_ID>"
```

### Method B: Wrangler CLI
From the repository root with `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` in the environment:
```bash
# 1. Inspect recent versions:
npx wrangler versions list

# 2. Deploy specific version to 100% traffic:
npx wrangler versions deploy "<VERSION_ID>@100%" --message "Manual rollback to <VERSION_ID>" --yes
```

### Method C: Helper Script
```bash
# List versions:
python3 scripts/ci/rollback.py --list

# Rollback:
python3 scripts/ci/rollback.py --version-id "<VERSION_ID>" --message "Rollback via helper" --yes
```

### Active Version Reference
- **Known baseline working version**:
  - `version_id`: `cf8aef89-43de-4732-a2aa-e34ce91a426d`
  - `tag`: `0197eaf0b3d2`
  - `source_commit`: `0197eaf0b3d2db5badf4f6004f86b1ca7c5b65c2`
  - `metadata_created_on`: `2026-10-17T17:25:42.263532Z`

> [!CAUTION]
> **Production Coordination Rule**:
> Do not execute production rollbacks without coordinating with root/head (`platform`). Live deployments must remain serialized with ongoing auth and session control work.

---

## 7. Local Validation Runbook

Developers and agents can run static validations locally before committing:
```bash
# 1. Validate workflow YAML, triggers, permissions, and security constraints:
python3 scripts/ci/validate-workflow.py

# 2. TypeScript typecheck:
npm run typecheck

# 3. Dry-run worker bundle packaging:
npm run build

# 4. Run focused vitest suites:
npx vitest run test/human-auth.test.ts test/token-auth.test.ts test/session-control.test.ts test/fleet.test.ts
```
