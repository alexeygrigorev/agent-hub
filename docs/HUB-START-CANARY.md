# Hub start canary (plan item 1.3)

Answers one question continuously: can Hub start a session that reaches a pane, today?
It uses only public CLIs (`hub sessions start|list|stop`, `a capture`, `a list --json --running`).

## Probe (`scripts/hub-start-canary.sh`, logic in `scripts/hub_start_canary.py`)

1. `start`: `hub sessions start --workspace <ws> --name hub-canary-<mode>-<run> --key canary-<run> --prompt <brief>`.
   Workspace is `$HUB_CANARY_WORKSPACE` or `~/work/hub-canary` (created mode 0700; keep it out of git).
   The `hub` CLI has no tag/label option, so the canary identity is the `--name` prefix `hub-canary-`
   (use it to exclude canaries from picker stats and to auto-archive).
2. `running`: poll `hub sessions list --json` up to 180 s for state `running`.
3. `list_host_workspace`: the same row must carry a host (`host` or `machine_id`), a `workspace` and a `native_id`.
4. `capture`: `a capture <native_id>` must contain `CANARY-OK` beyond the echoed prompt text.
5. `stop`: `hub sessions stop <id> --key canary-<run>-stop --wait`.
6. `gone`: the native id must leave `a list --json --running` within 60 s.

A `finally` always stops the session it created (`...-cleanup` key). If that fails the verdict says
`cleanup: FAILED_needs_reconciliation`.

Modes: `--mode full` (default; no engine, no model flag, brief "Reply CANARY-OK and exit"; timer every 30 min and
after each deploy) and `--mode shell` (`--engine shell --reason ...`, brief `echo CANARY-OK; exit`; no model, timer
every 5 min). No provider credential gate is invented.

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

`scripts/hub-start-canary.sh --deploy-gate` runs the full-mode canary once. It sends no mail and touches no alert
state. Exit 1 prints the JSON verdict on stdout and stderr, e.g.
`{"ok": false, "stage": "running", "reason": "state_failed:engine_not_allowed", "mode": "full", "deploy_gate": true, ...}`.
`deploy.yml` is not edited here; the head wires the last deploy step to run this and trigger the existing rollback on non-zero exit.
Allow ~6 min worst case. Note the VM that runs deploy needs the `hub`/`a` logins; the canary must run on a host with
`a` (Hetzner), e.g. over SSH.

## Install

`sudo bash scripts/install-hub-start-canary.sh [--user NAME]` (idempotent; installs scripts to
`/usr/local/sbin/hub-start-canary/`, four units to `/etc/systemd/system/`, enables both timers).
`bash scripts/install-hub-start-canary.sh --verify` renders units into a temp dir and runs `systemd-analyze verify`
without installing anything.
Units: `deploy/hub-start-canary.{service,timer}` (shell, 5 min), `deploy/hub-start-canary-full.{service,timer}` (30 min).

## Tests

`host/tests/test_hub_start_canary.py`; fake `hub` and `a` replay real output shapes. Run:
`scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- env PYTHONPATH=host python3 -m unittest host.tests.test_hub_start_canary`.

## OPEN QUESTIONS

- Shell mode through the public CLI: `hub sessions start --engine shell` needs the server to accept the shell engine with
  `--reason` and `--prompt` (AGENTS audit lists `shell_reason_required`; whether Hub2 launches an engine-less shell
  pane and how a prompt is typed into it is unverified). If the server refuses, the shell canary fails at `start`
  with the typed CLI exit reason, which is the honest signal. No host special case is faked.
- As of the audit the server's session row omits `workspace` (and the CLI shows empty HOST/WORKSPACE); the canary
  fails at `list_host_workspace` until the server reports them. Intended.
- Capture uses the row's `native_id`; whether headless (non-interactive) sessions keep a pane for `a capture` is unverified.
