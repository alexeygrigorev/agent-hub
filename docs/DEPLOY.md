# Agent Hub deployment

Production runs on the Hub2 Node service on the VM and is published at
`https://hub.dtcdev.click`. The current topology, rollout, backup, and rollback
instructions live in [Node VM deployment](NODE-VM-DEPLOY.md).

The Cloudflare Worker deployment is gone: the Workers were deleted on
2026-11-21 and the workers.dev URL no longer exists. There is no Wrangler
deploy path. Use `https://hub.dtcdev.click` for the CLI, MCP clients, and API
requests.

For a source change, use the current CI/CD path in the Node VM runbook: open a
PR, wait for required checks, merge, then run the production deploy workflow.
Verify `https://hub.dtcdev.click/health` after deployment.
