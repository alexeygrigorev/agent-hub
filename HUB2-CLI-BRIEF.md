# Canary 1 brief: Hub2 CLI/MCP/dashboard compatibility (Alexey-authorized direct launch, Hub bypass)

You are an independent Hub2 implementation worker on Hetzner. Hub API is DOWN (404) — no Hub placement; you run via aplexer. Request recorded in pending ledger; migrates to Hub intake on recovery. Hub2 coordinator ALONE integrates and prod-deploys — you implement your modules only, never deploy.

## Base and rules
- Worktree /home/alexey/git/agent-hub-wt-hub2-cli-1104, branch agent-hub-wt-hub2-cli-1104, explicit base hub-2.0 @ 09bde6ca44143a2e4be4e0b1e4ef1846510ff0d9. NEVER remove the worktree. Coordinator's in-flight edits land on hub-2.0; rebase only when coordinator says so.
- Read repo AGENTS.md + docs/hub2/FEATURES.md + docs/hub2/COST-CONTROL.md first; preserve root AGENTS instructions. No old-TBM/reward/auto-reset scope (expressly dropped).
- Quota: zai-central is LOW (~3% user-reported). If your provider calls start failing with exhaustion/quota errors: STOP new work immediately, checkpoint files, report HOLD — never silently switch provider or burn another account. No automatic reset redemption (Alexey resets).

## Scope (full source IDs + dedup)
Implement canonical CLI/MCP/dashboard compatibility against the Hub2 API so original AC holds, preserving IDs/AC mapping (full AC in ~/tmp/agent-reports/hub2-backlog-relevance.json):
- 08658ee8 intake list paging (no snapshot-changed dead-ends; MCP parity)
- 166af2de agents read/report own intake (URL/token via private config; deny foreign)
- 008529b5 / 3bdf9715 / a7c57b6e messaging M2/M5/M6 (per-session credential, CLI+MCP msg tools, named agents; dedupe overlapping clauses, one coherent implementation)
- 0992f366 sessions list/show machine_id consistency
Dedupe overlapping requirements into one implementation; document supported ops + limitations.

## Required
- Private pending-task record while Hub down: `.local/pending-cli-compat.json` (scope, IDs, status, evidence).
- First action: write session identity + `git log --oneline -1` + relevant FEATURES section list to `.local/first-action-hub2-cli.json`, then implement.
- Targeted tests for your modules; no full-suite runs that would strain quota. No secrets in prompts/argv/env/files. Never print credentials.
- Report: files changed, tests, limitations, integration notes for coordinator.
