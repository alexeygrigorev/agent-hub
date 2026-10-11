# Agent Hub

Serverless coordination hub for Alexey's multi-agent project: 30-minute headless root runs on a random available host, the daily writer job, machine registry, message bus, health and dashboard. The spec is [docs/SPEC.md](docs/SPEC.md); it is the source of truth for scope and milestones.

## Read first

- `~/git/cloudflare-agent-git/AGENTS.md`, `_docs/04-communication.md`, `_docs/03-way-of-working.md` and your role file in `~/git/cloudflare-agent-git/_docs/team/` (head: `04-head.md`, implementer: `05-implementer.md`, reviewer: `06-reviewer.md`). This repo follows the same team process.
- `~/git/agent-quota-launcher/README.md` each time you start an agent; the commands can change.

## Team

- `agent-hub-head`: interactive head in aplexer on Hetzner. Owns this repo's tracker (GitHub issues), backlog, workers, reviewers, integration and pushes. Reports to the principal (`codex-principal`) and root.
- Implementers and reviewers: headless, started only through the Agent Quota Launcher, named `agent-hub-implementer-<task>` / `agent-hub-reviewer-<task>`. A reviewer always runs on a different model from the implementer of the exact version under review. Implementers and reviewers talk only to the head.

## Process

1. Work through Agent Branches, the project's own tool (`~/git/agent-branches`, CLI `agent-branches`). Each implementer works in its own Agent Branches workspace. No git worktrees or long-lived git branches.
2. Claim edit scope on the agents bus before editing; release when done. Claims are never written into documents.
3. A task is done when a different-model reviewer approved the exact version and the head integrated it.
4. Integrate by pushing accepted work straight to GitHub `main` in small focused commits (one per logical change). No pull requests. Keep Agent Branches and GitHub main in sync after every integration (fast-forward only, never force). Verify the push before reporting.
5. Tests for real code, not for docs. Run them before reporting.
6. Post each milestone's accepted result on the tracking issue (see the issue labelled `tracking`).

## Gates and limits

- Quota gates: fresh `quse` before every launch; Codex launches go through `~/git/cloudflare-agent-git/scripts/launch-codex.sh` (runs `scripts/quota-gate.py`, 15% reserve, fails closed). Never pick paid options or "usage credits" in agent menus.
- Resources: follow the launcher's gates (worker memory ≤ 1500 MiB, MemAvailable ≥ 10 GiB after reservation, disk ≥ 50 GiB, TMPDIR under `<repo>/.local/tmp`, never `/tmp`).
- Budget: total cloud budget is USD 5/month including any paid plan. Design for the Cloudflare Workers **Free** plan. No purchases, plan upgrades, billing changes, new accounts or keys without Alexey.
- **No deploys.** Build and test locally only (`wrangler dev`, miniflare, vitest, `python3 -m unittest`, and `wrangler deploy --dry-run --outdir dist` for local bundle validation without API/auth). Do not run `wrangler deploy` without `--dry-run`, `wrangler login`, create Cloudflare resources, or touch DTC AWS (Cognito) resources. Prepare the deploy kit; root asks Alexey for approval.
- Secrets never go into Git, issues or bus messages. Use `.dev.vars` (gitignored) for local dev values. Private evidence lives in gitignored `.local/`.
- Do not stop, edit or replace the services the hub will retire (Agent Bus adapter :8788, desktop-orchestrator, supervision, principal-ping, agent-dashboard). Migration happens per docs/SPEC.md §12 with acknowledged handoffs.
- Heads own their commits; never overwrite, reset or delete another agent's work.

## Layout (target)

- `src/` Worker + `Hub` Durable Object (TypeScript)
- `public/` dashboard (static)
- `host/` Python host agent and `hub` CLI (stdlib only)
- `test/`, `host/tests/` tests
- `scripts/` deploy kit (prepared, not run), registry import
- `docs/` spec, deploy runbook
