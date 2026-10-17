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
