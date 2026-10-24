# Agent Hub scope

Purpose: turn Alexey's requests into tracked agent work: acceptance criteria (AC),
model/account/machine selection, managed sessions with receipts, and evidence for acceptance.

This repo: the private agent-work control plane, host adapters, CLI/MCP and Hub web UI.

## In scope

- Intakes and AC, operator rules and grants, routing tiers, quota admission and pacing,
  engine-account provisioning, and the machine registry for execution eligibility.
- Managed session start/send/capture/stop, receipts, custody, lineage and cleanup;
  bus messages, conversations, work alerts, metrics, Hub MCP and the Hub web UI,
  including use from a phone browser.
- Hub may stop or delete ANY session on a machine, including a manually started
  PocketShell/psc session (Alexey, t1405u). Who started a session does not exclude
  it from Hub's stop/delete authority; authentication and existing safety gates apply.
- Bounded capture snapshots and task artifacts are allowed as work evidence.
  Repeated capture must not become a hidden live terminal stream.
- Existing unlinked interactive start remains an operator/debugging facility.

## NOT our job (owner)

- Live terminal transport, PTY streams, raw keystrokes, xterm, resize or attach
  proxies — owner: PocketShell for human access; aplexer for the local session runtime.
- SSH tunnels, NAT relays, port forwarding, SFTP and a host file browser — owner: PocketShell.
- SSH keys, vault, settings sync, gateway/broker tokens and PocketShell login — owner: PocketShell.
  A PocketShell identity grants no Hub authority.
- Native mobile/desktop terminal apps, voice input and IME — owner: PocketShell.
- Session runtime internals, native identity/incarnation, local engine/profile definitions,
  hooks and state-directory/path logic — owner: aplexer; Hub consumes its published contract.
- A runtime dependency on PocketShell or shared product login/token storage — owners:
  Hub and PocketShell independently maintain their own availability, identity and credentials.
- A public multiuser SaaS connector platform — owner: a separately approved product,
  outside Agent Hub's private agent-work scope.
- Business MCP integrations — owner: a separate DataOps MCP server for DataOps (F10);
  Dapier/AISL tools are removed from the PocketShell MCP surface, with any future
  integration owned by its respective project after a separate decision.

## Interfaces and decisions

Hub publishes session/intake links and consumes the versioned aplexer CLI/RPC contract.
Optional links to PocketShell provide human terminal access without API calls,
credential exchange or a runtime dependency. Each product must work while the other is unavailable.
Delivery, exit zero and an agent saying "done" do not prove AC completion;
Hub owns acceptance verification.

2026-10-24: Hub owns managed agent sessions; manual human access = PocketShell.
The products stay separate (t1394u), Hub may stop/delete any session (t1405u),
and overlapping PocketShell automation is removed (t1406u–t1410u).
A gray-area change needs an ADR here with its intake ID and Alexey's approval.
