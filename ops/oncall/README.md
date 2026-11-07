# oncall-engineer

`oncall-engineer` is the persistent Hetzner session for alert triage and
incident routing across Alexey's infrastructure. It receives messages through
`~/bin/oncall-send`, which saves them under
`~/tmp/agent-reports/oncall/inbox/` before attempting pane delivery. Exit code
2 means the message is saved but no live session received it yet.

Read the [on-call and orchestrator architecture](../../docs/ONCALL-ORCHESTRATOR-ARCHITECTURE.md)
for the purpose of the session, the current Developer-mediated alert path,
the intended direct route, the watchdog's recovery role, and the intended
oncall model rule. This is an ops role for services beyond the Hub product.

Role instructions: [`CLAUDE.md`](CLAUDE.md). Hourly check:
[`hourly-check.md`](hourly-check.md). Start or restart it with
[`start.sh`](start.sh). The combined operator procedure is in
[`../orchestrator/README.md`](../orchestrator/README.md).
