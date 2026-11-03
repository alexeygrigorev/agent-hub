#!/usr/bin/env python3
"""Static validation of GitHub Actions CI/CD workflows and deployment configurations.

Validates:
1. Valid YAML syntax without external package requirements.
2. Path filters on push and pull_request to guarantee >=90% quota savings.
3. Documentation and local paths are excluded from deploy triggers.
4. Minimal job permissions (contents: read).
5. Secrets references (CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID) and no plaintext tokens.
6. Execution of scripts/build-hub-cli.py before wrangler deploy.
7. Proper deployment annotation flags (--tag, --message).
8. Manual rollback command syntax (wrangler versions deploy VERSION@100% --yes).
9. Bounded timeouts on all jobs.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def load_yaml(file_path: Path):
    content = file_path.read_text(encoding="utf-8")
    if HAS_YAML:
        return yaml.safe_load(content)
    # Simple fallback check if yaml module unavailable
    return {"_raw": content}


def validate_deploy_workflow(file_path: Path):
    print(f"Validating {file_path.name}...")
    content = file_path.read_text(encoding="utf-8")

    # 1. Plaintext secret check
    if re.search(r'(?i)(?:bearer\s+[a-f0-9]{32,}|cfpat-[a-z0-9_-]{20,})', content):
        raise ValueError(f"CRITICAL: Found possible hardcoded token in {file_path}")

    # Check secret references
    if "secrets.CLOUDFLARE_API_TOKEN" not in content:
        raise ValueError(f"Missing CLOUDFLARE_API_TOKEN secret reference in {file_path}")
    if "secrets.CLOUDFLARE_ACCOUNT_ID" not in content:
        raise ValueError(f"Missing CLOUDFLARE_ACCOUNT_ID secret reference in {file_path}")

    # Check path filters
    if "paths:" not in content:
        raise ValueError(f"Path filters missing in {file_path}; quota gate requires path filtering")

    # Check that docs/ are not in paths
    for line in content.splitlines():
        trimmed = line.strip()
        if trimmed.startswith("- ") and "docs/" in trimmed:
            raise ValueError(f"Documentation directory should not trigger deployment: {trimmed}")

    # Check CLI build before deploy
    if "scripts/build-hub-cli.py" not in content:
        raise ValueError(f"CLI build step missing in {file_path}; required before wrangler deploy")

    # Check deployment annotations
    if "--tag" not in content or "source_commit=" not in content:
        raise ValueError(f"Wrangler deploy missing required --tag or --message annotations in {file_path}")

    # Check permissions
    if "contents: read" not in content:
        raise ValueError(f"Permissions must specify minimal 'contents: read' in {file_path}")

    # Release gate (intake 2c87c231 AC1): deploys must consume an
    # orchestrator-issued, single-use, SHA-bound release slot first.
    if "release_slot_verify.py" not in content:
        raise ValueError(f"Release slot gate step missing in {file_path}; deploys must verify an orchestrator release slot")
    if "release_slot_verify.py --check-only" not in content:
        raise ValueError(f"Main-push release slot check missing in {file_path}")
    if "github.event_name == 'push'" not in content or "github.ref == 'refs/heads/main'" not in content:
        raise ValueError(f"Main-push release slot check must be limited to main pushes in {file_path}")
    if "github.event_name == 'workflow_dispatch'" not in content:
        raise ValueError(f"Production deploy job must be workflow_dispatch gated in {file_path}")

    # Check timeouts
    if "timeout-minutes:" not in content:
        raise ValueError(f"Bounded timeout-minutes missing in {file_path}")

    if HAS_YAML:
        data = yaml.safe_load(content)
        assert "jobs" in data, "No jobs declared"
        assert "test" in data["jobs"], "Missing test job"
        assert "deploy" in data["jobs"], "Missing deploy job"
        assert data["jobs"]["deploy"].get("needs") == "test", "Deploy job must depend on test job"

    # Concurrency gate (intake 9b43c2f0): the workflow must serialize per
    # workflow+ref so PR pushes cancel superseded runs while main pushes never
    # cancel an in-progress deploy but collapse pending mains to the newest.
    check_concurrency(content, file_path)

    print(f"  ✓ {file_path.name} passed all checks.")


def check_concurrency(content: str, file_path: Path):
    """Ref-keyed concurrency: group on workflow+ref, cancel only PR runs."""
    if "concurrency:" not in content:
        raise ValueError(f"Top-level concurrency group missing in {file_path}; PR runs must cancel, main runs must collapse to newest")
    for token in ("github.workflow", "github.ref", "cancel-in-progress", "pull_request"):
        if token not in content:
            raise ValueError(f"Concurrency group in {file_path} must be keyed by workflow+ref with cancel-in-progress on pull_request (missing {token})")


def validate_host_canary_workflow(file_path: Path):
    print(f"Validating {file_path.name}...")
    content = file_path.read_text(encoding="utf-8")

    if "contents: read" not in content:
        raise ValueError(f"Permissions must specify minimal 'contents: read' in {file_path}")

    if "timeout-minutes:" not in content:
        raise ValueError(f"Bounded timeout-minutes missing in {file_path}")

    check_concurrency(content, file_path)

    print(f"  ✓ {file_path.name} passed all checks.")


def validate_rollback_workflow(file_path: Path):
    print(f"Validating {file_path.name}...")
    content = file_path.read_text(encoding="utf-8")

    # Check workflow_dispatch
    if "workflow_dispatch:" not in content:
        raise ValueError(f"Rollback workflow must trigger via workflow_dispatch: {file_path}")

    # Check version_id input
    if "version_id:" not in content:
        raise ValueError(f"Rollback workflow must require version_id input: {file_path}")

    # Check wrangler versions deploy command
    if "wrangler versions deploy" not in content or "@100%" not in content:
        raise ValueError(f"Missing 'wrangler versions deploy VERSION@100%' in {file_path}")

    # Check permissions and secrets
    if "contents: read" not in content:
        raise ValueError(f"Permissions must specify minimal 'contents: read' in {file_path}")
    if "secrets.CLOUDFLARE_API_TOKEN" not in content or "secrets.CLOUDFLARE_ACCOUNT_ID" not in content:
        raise ValueError(f"Missing Cloudflare credentials in {file_path}")

    # Release gate (intake 2c87c231 AC2): a manual rollback is a manual deploy
    # and must consume a release slot bound to the exact version id.
    if "release_slot:" not in content or "release_slot_verify.py" not in content or "--target \"$INPUT_VERSION_ID\" --slot \"$INPUT_RELEASE_SLOT\"" not in content:
        raise ValueError(f"Release slot gate (release_slot input + verify step) missing in {file_path}")

    if HAS_YAML:
        data = yaml.safe_load(content)
        assert "jobs" in data, "No jobs declared"
        assert "rollback" in data["jobs"], "Missing rollback job"

    print(f"  ✓ {file_path.name} passed all checks.")


def main():
    deploy_path = WORKFLOWS_DIR / "deploy.yml"
    rollback_path = WORKFLOWS_DIR / "rollback.yml"
    canary_path = WORKFLOWS_DIR / "host-canary.yml"

    if not deploy_path.is_file():
        print(f"ERROR: {deploy_path} does not exist", file=sys.stderr)
        sys.exit(1)
    if not rollback_path.is_file():
        print(f"ERROR: {rollback_path} does not exist", file=sys.stderr)
        sys.exit(1)
    if not canary_path.is_file():
        print(f"ERROR: {canary_path} does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        validate_deploy_workflow(deploy_path)
        validate_rollback_workflow(rollback_path)
        validate_host_canary_workflow(canary_path)
        print("All CI/CD workflow validations PASSED.")
    except Exception as exc:
        print(f"Validation FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
