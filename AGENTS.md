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
4. Verification before every push (Alexey 2026-10-17 22:54): run the FULL test suite on Hetzner before every push (`scripts/test-full-local.sh` covering unfiltered Vitest discovery, full Python discovery with clean disposable HOME and DTC_AUTH_HOME, workflow validations, dry-run build). Investigate and report all failures honestly; required CI checks must pass with zero new regressions, and documented pre-existing debt is tracked rather than silently excluded. Never rely on focused-only tests or speculative CI runs.
5. Batch related commits into one finished feature. Nondeploy/docs/checkpoint commits must use `[skip ci]`. Exactly ONE deploy push for the finished feature, only after required CI test suites pass on the exact final source. Normal fast-forward integration, no force pushes, no PRs. Keep Agent Branches and GitHub main in sync after every integration.
6. Post each milestone's accepted result on the tracking issue (see the issue labelled `tracking`).

## Gates and limits

- Quota gates: fresh `quse` before every launch; Codex launches go through `~/git/cloudflare-agent-git/scripts/launch-codex.sh` (runs `scripts/quota-gate.py`, 15% reserve, fails closed). Never pick paid options or "usage credits" in agent menus.
- Resources: follow the launcher's gates (worker memory ≤ 1500 MiB, MemAvailable ≥ 10 GiB after reservation, disk free after launcher reservations/spike ≥ the validated Hub `resource_settings.disk_floor_gb` (default 15 GiB), TMPDIR under `<repo>/.local/tmp`, never `/tmp`).
- Reviewer-launch policy follows the configured placement disk floor (9af3a16), while preserving any stricter maintained-launcher runtime gate; a separate fixed 50 GiB policy would reject hosts that the validated resource setting admits. Read the current setting before each launch; missing or invalid configuration must fail closed. This policy alignment does not authorize disk cleanup or bypass any runtime gate.
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

## Model routing rule (2026-10-14)

OpenCode (`oc`, `--engine opencode`) always runs Muse Spark 1.3 (`opencode-go/muse-spark-1.3-contributor`), never a GLM / Z.ai Coding Plan model. All GLM / Z.ai work goes through zcodex (`--profile zcodex`, launcher `zai` route).

## Local test storage

Before installing dependencies in a fresh checkout, run
`python3 scripts/local-dependencies.py` to reuse a validated matching canonical
installation. `scripts/test-full-local.sh` runs this check automatically. Do not
run `npm ci` in each worktree when matching dependencies already exist; each copy
can add hundreds of megabytes. When manifests change, choose a matching donor
with `--source` or deliberately prepare one installation for that lockfile.
Never replace or remove another checkout's dependencies.

The full runner gives each run a unique temporary directory and zero pytest
temporary-file retention, removing only that run's new temps on exit. Existing
scratch, caches and session data need explicit owner approval before cleanup.
See [local test storage](docs/LOCAL-TEST-STORAGE.md).

## Throwaway containers

Use `docker run --rm` for every throwaway/test container and attach a unique
`--label agent.session=<Hub session id>` (or the native session id registered in Hub).
For `docker create` and Compose test workflows, attach the same label. Register
cleanup in a shell `trap` or `try/finally` before creating resources, and remove
every resource you create before the session exits, including failed starts.
Never remove another session's resources or Coolify-managed containers.

## Scheduled wakes (Hub sessions waiting on external events)

Waiting on an external event = register a Hub wake; a passive timer without a
wake is not allowed. The interim "aplexer wake not installed, use a transient
timer" path is retired for Hub sessions: use
`hub sessions wake <session> --at <ISO|now> (--prompt TEXT | --body-file F) [--key K]`
(REST `POST /api/sessions/:id/wake`, MCP `sessions_wake`). The wake is durable,
links to the session intake, shows as `waiting_until`, suppresses stall/nudge
until due, then delivers via the verified send path with a 10-minute ack window;
misses open a needs_attention alert + intake note and hand off to the
idle-with-open-AC path. Watchers end with `--at now --body-file <summary>` to
wake their owner with the result.
