# On-call and orchestrator architecture

This page explains why the two persistent operations sessions exist, how they
receive work, and who keeps them alive. It describes the Hetzner operating
setup as of 2026-11-07; the linked runbook and scripts remain the source for
commands and implementation details.

## Why this exists

Operations and the Hub work queue should keep moving when Developer (the Grok
Bot overseer) is away or out of tokens. Developer coordinates and forwards
work; the two sessions below do the investigation and queue work. They live in
the `agent-hub` repository because it owns their operational contracts, start
scripts, send commands, and watchdog. These routines cover services beyond the
Hub product itself.

## The two sessions

| Session | What it does | Workspace and tag | Reports and forwarding |
|---|---|---|---|
| `orchestrator-main` | Drives Hub and queue work: opens intakes, starts fixers, and keeps ready work moving. | `~/git/agent-hub-wt-orchestrator-run`, working directory `ops/orchestrator`; tag `orchestrator-main`. | `~/tmp/agent-reports/orchestrator/{latest.md,NEEDS_ALEXEY.md}`; `~/bin/orch-send`. |
| `oncall-engineer` | Triages alerts and routes incidents across dev@ mail, Relay transaction monitoring, Coolify/Contabo, CloudWatch, and CI. | `~/git/agent-hub-wt-oncall-run`, working directory `ops/oncall`; tag `oncall-engineer`. | `~/tmp/agent-reports/oncall/{latest.md,NEEDS_ALEXEY.md,inbox/}`; `~/bin/oncall-send`. |

Both are persistent interactive sessions started by the official scripts
[`ops/orchestrator/start.sh`](../ops/orchestrator/start.sh) and
[`ops/oncall/start.sh`](../ops/oncall/start.sh). Their role instructions and
hourly checks are in `ops/orchestrator/` and `ops/oncall/`.

The send commands save each message into the role's inbox before trying to
deliver it to the live pane. For `oncall-send`, exit code 2 means the message
was saved but not delivered because no live session was available; the inbox
keeps it for the next check or restart. `orch-send` uses the same durable-first
pattern. The `latest.md` files are the current status; `NEEDS_ALEXEY.md` is
reserved for decisions that need Alexey, such as production approvals, 2FA, or
deletions.

## Alert flow: current and intended

The current route still passes through Developer:

```text
infrastructure mail -> dev@dtcdev.click -> Dapier -> Grok Bot routine
"Dapier/dev mailbox wake" -> Developer -> oncall-send -> oncall-engineer
```

Developer usually also uses `orch-send` when the alert affects Hub queue work.
If Developer is unavailable, an alert can stall before it reaches
`oncall-engineer`. The send command then protects messages from being lost
after Developer has forwarded them, but it cannot bypass that earlier hop.

The intended route sends new alerts from Dapier directly to `oncall-engineer`,
without waiting for Developer. A separate implementation is in progress. Do
not treat this route as live until its end-to-end delivery is verified; the
current Developer-forwarded route remains the operational path until then.

## Cadence, recovery, and Developer's role

Hourly work is currently driven inside the live sessions by Claude `/loop` as
a stopgap. The target is Hub scheduled prompts attached to the session tags,
so cadence does not depend on Claude or another particular engine. The
checked-in start controller sends a one-time kickoff and describes Hub
schedules as the recurring mechanism; confirm the live schedule and its
deliveries before considering the move complete.

The user-level `ops-watchdog.timer` runs about every 10 minutes and invokes
`ops-watchdog.service`, which calls the installed
`~/.local/share/agent-hub-ops/watchdog.sh`. The watchdog checks the sessions;
it is a recovery check, not a periodic prompt:

- For `orchestrator-main`, it restarts a dead session or one with no fresh
  work tick, then expects the restarted session to produce a tick.
- For `oncall-engineer`, it restarts a dead session and recovers a live one
  stuck on a provider limit screen or with an unprocessed inbox item older
  than 20 minutes.
- A healthy live session is left alone. Oncall is checked for inbox progress;
  the orchestrator is checked for tick progress.

The unit files live in `~/.config/systemd/user/ops-watchdog.{timer,service}`;
their source and the recovery logic are in
[`ops/systemd/`](../ops/systemd/) and [`ops/lib/`](../ops/lib/). The host
runbook has the detailed proof windows and operator commands.

Developer's role is overseer, not investigator: forward Alexey's messages and
alerts, start or restart a session when needed, check that its pane shows real
work, and hand it the right instructions. `oncall-engineer` investigates
incidents; `orchestrator-main` drives Hub queue work. Developer relays
`NEEDS_ALEXEY.md` when a decision truly belongs to Alexey.

## Intended oncall model rule

Oncall should use a cheaper daily model. The intended rule is no Argon, Astra,
Opus, or high effort for oncall. A separate start-path change is in progress;
this policy describes the target and does not establish which model the live
session currently uses.

## Pointers

- [Operations runbook](../ops/orchestrator/README.md): install, start, attach,
  send, watchdog, and handoff procedures.
- [Oncall role README](../ops/oncall/README.md): alert-triage entry points.
- [Hub scheduled prompts](PROMPT-SCHEDULES.md) and
  [Hub webhooks](WEBHOOKS.md): Hub-side delivery mechanisms and their status.
