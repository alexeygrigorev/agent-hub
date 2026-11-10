# Agent Hub

Node/SQLite coordination hub for Alexey's multi-agent project: 30-minute headless root runs on a random available host, the daily writer job, machine registry, message bus, health and dashboard. The spec is [docs/SPEC.md](docs/SPEC.md); it is the source of truth for scope and milestones.

## Read first

- `~/git/cloudflare-agent-git/AGENTS.md`, `_docs/04-communication.md`, `_docs/03-way-of-working.md` and your role file in `~/git/cloudflare-agent-git/_docs/team/` (head: `04-head.md`, implementer: `05-implementer.md`, reviewer: `06-reviewer.md`). This repo follows the same team process.
- `~/git/agent-quota-launcher/README.md` each time you start an agent; the commands can change.

## Team

- `agent-hub-head`: interactive head in aplexer on Hetzner. Owns this repo's tracker (GitHub issues), backlog, workers, reviewers, integration and pushes. Reports to the principal (`codex-principal`) and root.
- Implementers and reviewers: headless, started only through the Agent Quota Launcher, named `agent-hub-implementer-<task>` / `agent-hub-reviewer-<task>`. A reviewer may run on the same model as the implementer of the exact version under review. Implementers and reviewers talk only to the head.

## Process

1. Start from `origin/main` in an isolated task worktree and short-lived PR branch. Preserve other agents' work; never push directly to main or force-push. This Git/PR flow supersedes older Agent Branches-only and no-PR instructions in inherited process documents.
2. Claim exact edit scope on the agents bus before editing and release it after handoff. Shared files require an acknowledged ownership handoff. Claims are never written into documents.
3. Run only targeted tests for changed files locally. Before pushing, run `scripts/test-affected-local.sh`; if its selector requires a broad/full suite, run that suite in CI. Never run `scripts/test-full-local.sh` or broad suites on Hetzner. Use the heavy-test admission controls below for targeted commands that require them. See [targeted checks](docs/PREPUSH-TARGETED-TESTS.md).
4. Push the finished feature branch and open a PR. CI runs the required full suites and Python host/CLI checks. Start-path acceptance additionally requires the integrated real-server/daemon/CLI end-to-end harness to be wired as a required CI check. Report every failure honestly and fix new regressions. Do not skip required CI on a mergeable PR.
5. Obtain independent review of the exact final head SHA (same model allowed). Any source change invalidates the earlier review. After review and green required CI, enable rebase auto-merge with `gh pr merge --auto --rebase --match-head-commit <reviewed-sha>`. The head owns integration and accepted-result tracking; implementation is not done until the reviewed version is integrated.
6. Deployment is a separate CI operation owned by the designated deploy owner. Deploy only the accepted main commit, check the live start canary and roll back on failure. Workers do not dispatch deploys, install from worktrees into live environments or replace live units. Post each accepted milestone result on the tracking issue.

## Commit scope trailers

Agent Hub installs a `commit-msg` hook before Hub-launched sessions start. It
adds `Scope: in` when absent, preserves a valid `Scope: gray <intake-UUID>`,
and runs for merge commits on Linux and Windows. A direct aplexer workspace
that did not go through Hub bootstrap must run
`python3 host/agent_hub_host/prepush.py install-scope` (Linux) or
`python host/agent_hub_host/prepush.py install-scope` (Windows) before
committing. Never bypass the hook with `--no-verify`; CI still rejects
sensitive commits that reach it without a valid final trailer.

## Gates and limits

- Quota gates: fresh `quse` before every launch; Codex launches go through `~/git/cloudflare-agent-git/scripts/launch-codex.sh` (runs `scripts/quota-gate.py`, 15% reserve, fails closed). Never pick paid options or "usage credits" in agent menus.
- Resources: follow the launcher's gates (worker memory ≤ 1500 MiB, MemAvailable ≥ 10 GiB after reservation, disk free after launcher reservations/spike ≥ the validated Hub `resource_settings.disk_floor_gb` (default 15 GiB), TMPDIR under `<repo>/.local/tmp`, never `/tmp`).
- Reviewer-launch policy follows the configured placement disk floor (9af3a16), while preserving any stricter maintained-launcher runtime gate; a separate fixed 50 GiB policy would reject hosts that the validated resource setting admits. Read the current setting before each launch; missing or invalid configuration must fail closed. This policy alignment does not authorize disk cleanup or bypass any runtime gate.
- Billing: the hub no longer uses Cloudflare; there is no Workers cost envelope, billing backstop or 0.03 USD/day alert to maintain. AWS sandbox spend follows the aws-infra repo's existing guardrails; do not create paid services beyond the documented stacks without separate approval.
- Deployment: local-only defaults (`vitest`, `python3 -m unittest`, `node --experimental-transform-types hub2/node/main.ts` smoke, targeted suites). Production = the hub VM via `deploy/vm/install-hub2-node.sh` (Node 24 runtime, SQLite under /var/lib/agent-hub/hub2-db, systemd `agent-hub-hub2.service`, caddy edge) and DNS/edge in the aws-infra repo (`sandbox/hub-edge` origin, `sandbox/agent-hub` records). CI deploys via SSH only; never reintroduce Cloudflare or wrangler.
- Secrets never go into Git, issues or bus messages. Use `.dev.vars` (gitignored) for local dev values. Private evidence lives in gitignored `.local/`.
- Do not stop, edit or replace the services the hub will retire (Agent Bus adapter :8788, desktop-orchestrator, supervision, principal-ping, agent-dashboard). Migration happens per docs/SPEC.md §12 with acknowledged handoffs.
- Heads own their commits; never overwrite, reset or delete another agent's work.

## Layout

- `hub2/src/`: Hub2 API and coordination objects (TypeScript).
- `hub2/node/`: production Node 24 runtime, HTTP/WebSocket edge and SQLite persistence.
- `hub2/public/`: dashboard and generated `/cli/` release artifacts.
- `hub2/host/`: Python Hub2 daemon and native administration CLI; isolated release environments pin the approved `websockets==16.0` transport. No other unpinned dependencies are authorized.
- `host/agent_hub_host/`: maintained stdlib CLI zipapp source and legacy Hub1 modules pending reviewed retirement. Both packages currently share an import name; keep their environments isolated.
- `hub2/tests/`, `hub2/host/tests/`, `host/tests/`: server, daemon and CLI tests; integrated end-to-end coverage runs in CI.
- `src/`, `test/`, `public/`: legacy Hub1 code/assets retained until acknowledged retirement; `public/cli/` includes maintained CLI installers.
- `scripts/`, `deploy/`: release tooling, user/system units, and VM deployment. Hub owns only `/etc/caddy/sites/hub.caddy`; aws-infra owns the shared Caddyfile and its sites import.
- `docs/`: spec and runbooks, including [host releases](docs/HUB2-RELEASES.md).

## Model routing rule (2026-10-14)

OpenCode (`oc`, `--engine opencode`) always runs Muse Spark 1.3 (`opencode-go/muse-spark-1.3-contributor`), never a GLM / Z.ai Coding Plan model. All GLM / Z.ai work goes through zcodex (`--profile zcodex`, launcher `zai` route).

## Local test storage

Heavy suites share a host-wide cap of two runs per user. The full runner acquires it
automatically. `scripts/test-affected-local.sh` is the pre-push lane and takes its own slot. Wrap ad-hoc Vitest/workerd (including `npm test`), Playwright and
full pytest commands with `scripts/capped-test-run.sh -- <command>`; use the
same `AGENT_HUB_HEAVY_TEST_DIR` across sessions (the live `<dir>/slots` file
inside it is the single host-level slot count -- do not export
AGENT_HUB_HEAVY_TEST_SLOTS per-process; it is only a fallback for private pools
and tests when the file is absent).
Defaults are `/run/user/<uid>/agent-hub/heavy-test`, six slots (live `<dir>/slots`
file may override, hard ceiling 8), and a 1800-second
wall-clock cap per admitted command (`--run-timeout` or
`AGENT_HUB_HEAVY_TEST_RUN_TIMEOUT_S` overrides the run cap). Never unlink
live lock files or kill holders. See [heavy test admission](docs/HETZNER-OVERLOAD-0689CE9C.md).
If the affected lane prints a capacity HOLD, retry it directly with
`scripts/capped-test-retry.sh -- scripts/test-affected-local.sh`; it inherits
`AGENT_HUB_HEAVY_TEST_PRIORITY` when set. Never nest the lane in another
admission wrapper or clear `AGENT_HUB_HEAVY_TEST_HELD` in a child shell.

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
