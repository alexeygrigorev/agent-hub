#!/usr/bin/env bash
set -euo pipefail

# M7 Deploy Kit - Agent Hub

echo "Agent Hub Deployment Script"

# Check arguments
APPROVED=false
DRY_RUN=false

for arg in "$@"; do
    case $arg in
        --i-have-approval)
            APPROVED=true
            shift
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: $0 [--dry-run] [--i-have-approval]"
            exit 1
            ;;
    esac
done

if [ "$DRY_RUN" = false ] && [ "$APPROVED" = false ]; then
    echo "ERROR: Actual deployment requires explicit root and Alexey approval."
    echo "Please run with --i-have-approval if you have explicit permission to deploy,"
    echo "or --dry-run for a local test."
    exit 1
fi

echo "Running checks..."

if [ ! -f "wrangler.jsonc" ] && [ ! -f "wrangler.toml" ]; then
    echo "ERROR: wrangler config not found. Must be run from the root directory containing M0 scaffold."
    exit 1
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "ERROR: npx not found."
    exit 1
fi

# We use npx --no-install, so the tool must be installed.
# We shouldn't use npm ci for production deploy in the script unless requested,
# but for local dry run it's fine or we assume npm install was run.
# The requirement: "npmci localonly; npx --no-install cannot fetch tools."
# We will run `npm ci` locally if it's a dry run, or just require it.
if [ "$DRY_RUN" = true ]; then
    echo "Executing dry run..."
    
    if [ ! -d "node_modules" ]; then
        echo "node_modules not found. Running npm ci locally..."
        npm ci
    fi
    
    mkdir -p .local
    echo "Running wrangler dry-run..."
    touch .local/evidence.log
    chmod 600 .local/evidence.log
    if ! npx --no-install wrangler deploy --dry-run --outdir .local/buildout 2>&1 | tee .local/evidence.log; then
        echo "Wrangler dry-run failed."
        exit 1
    fi
    
    echo "Dry run completed locally. Evidence saved to .local/evidence.log."
    exit 0
fi

if [ "$APPROVED" = true ]; then
    echo "Executing actual deployment..."
    echo "WARNING: This will affect the live Cloudflare environment."
    npx --no-install wrangler deploy
    echo "Deployment completed."
    exit 0
fi
