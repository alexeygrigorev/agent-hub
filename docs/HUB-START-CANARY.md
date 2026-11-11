# Hub start canary (plan item 1.3)

Answers one question continuously: can Hub start a session that reaches a pane, today?
It uses only public CLIs (`hub sessions start|list|capture|stop`, `a list --json --running`).

## Probe (`scripts/hub-start-canary.sh`, logic in `scripts/hub_start_canary.py`)

1. `start`: `hub sessions start --no-wait --repo <project> --machine <local-host> --workspace <ws> --name hub-canary-<mode>-<run> --key canary-<run> --prompt <brief>`.
   Workspace is `$HUB_CANARY_WORKSPACE` or `~/work/hub-canary` (created mode 0700; keep it out of git). Project defaults to `alexeygrigorev/agent-hub` (`HUB_CANARY_REPO`); machine defaults to `hetzner-aplexer` (`HUB_CANARY_MACHINE`). These timers run on that same local machine because native inventory uses its `a` binary. The returned host and workspace must match the request. The 180-second running deadline starts before the launch command; each subprocess is capped by the remaining stage time.
   The `hub` CLI has no tag/label option, so the canary identity is the `--name` prefix `hub-canary-`
   (use it to exclude canaries from picker stats and to auto-archive).
2. `running`: poll `hub sessions list --json` up to 180 s for state `running`.
3. `list_host_workspace`: the same row must carry a host (`host` or `machine_id`), a `workspace` and a `native_id`.
4. `capture`: `hub sessions capture <session_id> --json` must acknowledge actual output matching the expected marker. Headless local profiles on Linux use bounded stdout from their exact launch attempt; shell and interactive sessions retain pane capture. Explicit `--screen` capture always reads pane history. The host validates the task START record against the native parent, workspace, profile, prompt digest and launch time, and rejects symlink traversal. Full mode binds parser verification to the running session's `engine` and requires completed tool output in a single capture (Codex/ZAI completed `command_execution` with exit code 0; Go completed `tool_use`; Claude complete matching Bash `tool_use` and user `tool_result` with `is_error:false`, `interrupted:false`, empty `stderr`, and equal `stdout`/`content`; Antigravity complete matching `run_command` `step_update` `ACTIVE` and `DONE` frames with identical parameters and the anchored native exit-0 header). Assistant prose, `result` summaries, DONE-only/orphan frames, stitched captures, and prompt/command echoes never count. Full mode uses `CANARY-OK:<run-id>` and asks the model to concatenate separate tokens, so the complete marker never occurs in the prompt. Shell mode requires the exact `CANARY-OK` non-JSON output line.
5. `stop`: `hub sessions stop <id> --key canary-<run>-stop --wait`.
6. `gone`: the native id must leave `a list --json --running` within 60 s.

A `finally` always stops the session it created (`...-cleanup` key). If that fails the verdict says
`cleanup: FAILED_needs_reconciliation`.

Modes: `--mode full` (default; no engine, no model flag, a brief that completes one shell tool call to print a nonce-bearing marker, then uses the provider-native background tool facility for a separate bounded 60-second hold monitored internally for verified capture and stop; timer every 30 min and
after each deploy) and `--mode shell` (`--engine shell`, brief `printf CANARY-OK; sleep 30`; no model, timer
every 5 min). Shell contract (server, orchestrator-approved): engine=shell is headless only; the prompt is the literal OS command run via `/bin/sh -c` (Linux; Windows `cmd /d /s /c` unverified); no provider home; no reason or override needed (the canary sends no `--reason`). The 30 s sleep keeps the pane alive so capture and stop are observed. Capture passes only for the expected output line, allowing terminal gutter characters. No provider credential gate is invented.

Output: one JSON line on stdout (`ok, mode, run_id, timestamp, stage, reason, session_id, cleanup`), repeated on
stderr on failure. Exit 0 pass, 1 fail. Timeouts can be tuned with `HUB_CANARY_{RUNNING,CAPTURE,STOP,CMD}_TIMEOUT_S`,
`HUB_CANARY_POLL_S`; `HUB_BIN` / `A_BIN` replace the CLIs (tests).

## Alerting

State: `${HUB_CANARY_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/agent-hub-canary}` (dir 0700, files 0600), one state file per mode.
Two consecutive failures send ONE mail to `dev@dtcdev.click` (fixed in code; never orin@, no CC, not env-configurable)
through `msmtp -a alerts -f alerts@dtcdev.click`, the same path as `resource-gate-monitor.sh`. Subject and body carry
stage, typed reason, timestamp and run id. No repeat while the incident is open (also when failures continue or one
success interrupts them). After two consecutive successes one `[RECOVERED]` mail is sent and the incident closes.
A failed send is retried on the next run. Tests inject `HUB_CANARY_MAILER` (a command receiving the message on stdin)
and never call real msmtp.

## Deploy gate

`scripts/hub-start-canary.sh --deploy-gate` runs the full-mode canary once (needs an LLM start); `--deploy-gate --mode shell` runs the cheap shell canary instead (default stays full). It sends no mail and touches no alert
state. Exit 1 prints the JSON verdict on stdout and stderr, e.g.
`{"ok": false, "stage": "running", "reason": "state_failed:engine_not_allowed", "mode": "full", "deploy_gate": true, ...}`.
`deploy.yml` is not edited here; the head wires the last deploy step to run this and trigger the existing rollback on non-zero exit.
Allow ~7 min worst case, including bounded failure cleanup. Note the VM that runs deploy needs the `hub`/`a` logins; the canary must run on a host with
`a` (Hetzner), e.g. over SSH.

## Install

`sudo bash scripts/install-hub-start-canary.sh [--user NAME]` (idempotent; installs scripts to
`/usr/local/sbin/hub-start-canary/`, four units to `/etc/systemd/system/`, enables both timers).
`bash scripts/install-hub-start-canary.sh --verify` renders units into a temp dir and runs `systemd-analyze verify`
without installing anything.
Units: `deploy/hub-start-canary.{service,timer}` (shell, 5 min), `deploy/hub-start-canary-full.{service,timer}` (30 min).

## Tests

`host/tests/test_cli_hub_start_canary.py`; fake `hub` and `a` replay real output shapes. Run:
`scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- env PYTHONPATH=host python3 -m unittest host.tests.test_cli_hub_start_canary`.

## OPEN QUESTIONS

- Windows hosts: shell prompt via `cmd /d /s /c` is unverified.
- A deployed server must include the projection and CLI fixes from PR391; older releases omit workspace or lack `--no-wait`. The canary
  fails at `list_host_workspace` until the server reports them. Intended.
- Capture uses the row's `native_id`; whether headless (non-interactive) sessions keep a pane for `a capture` is unverified.

Ambiguous start receipts are retried once with the identical key and payload during bounded cleanup. A recovered session is stopped; repeated response loss is reported as `FAILED_needs_reconciliation`, never `not_needed`. Current CLI pagination is exhausted with `sessions list --all --json`; the aggregated `sessions` array takes precedence over any retained first-page `items`. Incomplete cursor results fail explicitly. Stage deadlines also cap capture, stop and native disappearance checks; late success does not pass.

Headless stdout capture uses descriptor-relative no-symlink file reads on Linux. Platforms without that primitive retain their existing native launch and pane capture behavior; headless file capture there remains unverified. A capture acknowledgement is limited to a 4096-byte stdout tail and an 8000-byte JSON payload, below the server command-result ceiling. No stderr or raw START/RESULT record is returned.
