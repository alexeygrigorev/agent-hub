# Agent Hub deployment

Production runs on the Hub2 Node service on the VM and is published at
`https://hub.dtcdev.click`. The current topology, rollout, backup, and rollback
instructions live in [Node VM deployment](NODE-VM-DEPLOY.md).

The former Cloudflare Worker deployment runbook is retired. The old
`agent-hub.cloudflare-ai-agent-de9ca0.workers.dev` URL is a bootstrap stub and
can return `503 release_bootstrap`; it is not a production client endpoint.
Do not deploy with Wrangler or point Hub clients at that URL. Use the custom
domain for the CLI, MCP clients, and API requests.

For a source change, use the current CI/CD path in the Node VM runbook: open a
PR, wait for required checks, merge, then run the production deploy workflow.
Verify `https://hub.dtcdev.click/health` after deployment.
