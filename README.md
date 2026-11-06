# Agent Hub 2

The canonical root package and deployment use Hub2: six SQLite Durable Objects,
a native aplexer host, and the authenticated CLI/dashboard.

Install with uv pip install -e .; hub and hub2 expose the same CLI.
Existing bots CLI root editable dependencies resolve this implementation.
Root wrangler.jsonc targets hub2/production-worker.ts and hub2/public.

Checks: npm test, npm run typecheck, npm run build, and
python scripts/ci/test_root_distribution.py. The reusable Hub2 workflow owns
the full behavioral and cost release gates.

Pushes validate; activation uses the manual Hub 2 production workflow.
Production defaults to maintenance. Bootstrap and approved budget controls
are documented in [docs/hub2](docs/hub2/).

The hub1 tag preserves the previous implementation. Never restore a Hub1
Worker version over the fresh Hub2 database. Rollback requires a reviewed
Hub2 source rebuild through the same release gates.

Credentials and controller configuration stay private. Never place them in
source, workflow inputs, arguments or native session metadata.
