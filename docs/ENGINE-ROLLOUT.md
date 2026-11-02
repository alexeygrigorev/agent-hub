# Hub-managed engine version rollout (intake 8cc9e31a)

Hub owns engine updates for `codex`, `claude`, `opencode`, `agy`/`antigravity`
and `zcodex` on every registered host. This reuses the reviewed installer pins
(`host/agent_hub_host/bootstrap_pins.py`) and the managed installer
(`host/agent_hub_host/provisioning.py`) — there is no parallel updater.

## Commands

- Daily check (scheduled): the Worker 5-minute cron runs
  `ProvisioningService.tickEngineRollout()`, internally rate-limited to about
  once per 20 hours. It compares the upstream latest (npm registry for
  codex/claude/opencode; reviewed pins for agy/zcodex) against the desired
  pins and raises at most one actionable `engine_rollout` alert:
  ``hub engines update --engine X``.
- On-demand: `hub engines update [--engine X] [--machine Y] [--canary HOST]`
  bumps the central bundle versions (new revision per label), canary first
  (default canary `RMTHZ`). `--check-only` reports without installing.
- Status: `hub engines versions [--fleet] [--machine M]` and
  `hub engines status` (now includes `engine_versions`) show per host per
  engine installed / desired / latest / last result, taken from a check run
  on that host (`host/agent_hub_host/engine_rollout.py::check_host`).

## Safety

- **Never interrupts running sessions (AC2):** updates install side by side
  (`<label>-<new-revision>`); the previous revision directory is kept while
  live sessions use it (`engine_rollout.deferred_labels()` consulted by
  `provisioning.sync()`). New sessions resolve the profile to the new
  revision.
- **Canary + real smoke (AC3):** the canary host runs `--version` plus one
  real prompt per engine (`engine_rollout.smoke_engine`). Green rolls the
  rest out (they sync on heartbeat); failure keeps/serves the previous
  version (automatic bundle rollback) and raises one actionable alert.
- **No self-update prompts (AC4):** Hub provisioning reconciles on every
  bootstrap and execution: `check_for_update_on_startup = "false"` for
  codex/zcodex, `autoUpdates = false` + `DISABLE_AUTOUPDATER=1` for claude,
  `autoupdate = false` for opencode, pinned-only binary + disable env for
  agy. Session env carries the same disables belt-and-suspenders.

## Version sources

Server desired pins live in `src/provisioning.ts`
(`rolloutDesired()`); host pins in `bootstrap_pins.py`. Update both in the
same change. `zcodex` tracks the reviewed codex CLI version.
