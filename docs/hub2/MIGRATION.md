# Hub 2.0 migration plan

Status: proposed sequence only, 4 November 2026. This file does not authorize a production migration. The current Hub is reported as disabled and returning a 503 stub. No services, routes, data, Cloudflare resources, or production state are changed in this architecture step.

## Data to carry forward

| Data | Migration treatment |
| --- | --- |
| Open and recently completed intake tasks | Preserve stable task IDs, original text, AC order, project, status, priority, created/updated timestamps, version, links, and event order. Copy open tasks first; retain recent closed tasks needed for dedup and follow-up. |
| Intake dependencies and blocker relationships | Preserve both endpoints and the blocking reason. Verify same-project and acyclic constraints before import. |
| Queued jobs, recurring schedules, and durable wakes | Preserve idempotency keys, next due time, timezone, payload type, and queue order. Recompute expired/due state at import; do not execute a row merely because it was copied. |
| Sessions and native aplexer bindings | Keep stable Hub session IDs and intake links. At cutover, compare every apparently live row against a fresh read-only native aplexer census. Import only confirmed live native UUIDs; mark stale rows as historical/unknown, never as running. |
| Machines, aliases, capabilities, and drain state | Preserve stable machine IDs, OS, verified capability labels, and operator drain state. Re-enroll each host with a new one-time credential and prove a fresh connection before it is eligible for placement. |
| Account labels, routing policy, and quota snapshots | Preserve account identifiers and reviewed model policy. Recompute quota snapshots at first operator read or placement; old snapshots are history, not fresh admission evidence. |
| Provider credentials | Migrate only as an encrypted, versioned bundle over an operator-approved private path. Do not place plaintext in Git, task reports, bus messages, or ordinary database exports. Revoke old host copies after the new generation is acknowledged. |
| Durable messages | Preserve unexpired messages, threads, sender/recipient identity, idempotency keys, and delivery stages. Reconcile undelivered session messages against the native aplexer inbox before replay to avoid duplicate prompts. |
| Work claims | Preserve only unexpired active claims and their project/scope/work key. Reconcile each against current aplexer work declarations and release stale claims with an audit event. |
| Human, host, agent, and MCP credentials | Do not copy active bearer/refresh tokens. Preserve operator identity allowlists and grant definitions; require human re-login, device re-approval, and MCP reauthorization. |
| Detailed health samples, unproven token/cost data, old UI projections | Do not migrate as live state. Keep unknown as unknown. Retain only bounded aggregate evidence that has a known source and an active dashboard consumer. |

All imported rows retain a source ID and source version until validation finishes. The export is immutable, private, encrypted where it contains provider credentials, and held outside public Git. No source row or service is deleted by the migration.

## Staged cutover

1. **Finish the local implementation and contract tests.** Build the new sharded DO classes, API/CLI/MCP compatibility, export/import tool, and budget instrumentation. Validate with disposable local SQLite/Miniflare data and a Wrangler dry run. Do not create a production namespace or deploy until Alexey approves.
2. **Inventory consumers and take a source snapshot.** Before a future cutover, read the then-current API/MCP telemetry, skill commands, host versions, active schedules, queues, and native session census. Use a consistent read-only export. Compare the export row counts with source summaries; keep the export and hash as the recovery point.
3. **Validate migration in isolation.** Import a sanitized copy into disposable local shards. Compare IDs, AC line order, event sequences, queue idempotency keys, account generations, message threading, capability labels, and session/native UUID links. Run contract and row-budget tests. Keep credentials synthetic in this test.
4. **Prepare clients and hosts.** Publish a compatible CLI package but do not change the production endpoint. Enroll test hosts with fresh device approval, verify their outbound WebSocket and state-report path, and verify that host-local launcher/quota gates still refuse unsafe starts.
5. **Freeze old writes at an approved window.** If the old control plane becomes writable before then, stop new starts and schedule mutations, drain or explicitly preserve queued rows, take a final snapshot, and record the queue cursor. Do not stop or rewrite any external service without the required acknowledged handoff.
6. **Import and verify.** Import the final snapshot idempotently. Re-run the native live-session census. Check the counts and representative records for every carried data class. Reconcile account generations, message delivery state, and active claims. Keep all import mappings and old data read-only.
7. **Switch endpoint only after explicit production approval.** Move the approved route to the new Worker version. Verify login, intake create/read, one test-host registration, one controlled session start/send/ack/stop, one message thread, and dashboard status. Do not enable the full root/writer schedules until these checks pass.
8. **Ramp schedules and fleet.** Enable the 30-minute root and daily writer schedules as explicit queue items, then enroll remaining hosts. Track aggregate rows_read plus rows_written by shard and API route each day. Keep the 20,000/day cap and queue-lag/error alarms active through the observation window.

The report that the current production endpoint is a 503 stub changes the migration starting point: do not assume there is a live old service to receive a reverse DNS switch. If it remains disabled, the “cutover” is a first activation of Hub 2.0 after the production approval gate, with a source-code/local export and read-only aplexer census rather than a live dual-write period.

## Rollback

The default rollback target is a pinned, previously validated Hub 2.0 Worker version with the same compatible DO schema. Before switching traffic, verify that this version can serve the migrated schema and keep the immutable import snapshot. Do not treat the legacy 503 stub as an operational rollback target unless Alexey separately approves restoring it.

If authentication, dispatch, data parity, or the 20,000-row-operation budget fails:

1. Disable new placement and schedule firing at the Worker/API layer; leave already running native sessions alone.
2. Preserve all Hub 2.0 databases, queues, saga steps, and exports. Do not delete namespaces or reset database state.
3. Reconcile every held or running dispatch against the owning host's native aplexer UUID and launcher result. Release a reservation only after its holder is confirmed stopped or its lease expires under the fencing rules.
4. Route traffic to the pinned Hub 2.0 version if its schema is compatible. If a different rollback target is proposed, first export all post-cutover intakes, messages, schedules, and idempotency keys for a reviewed replay; never blindly re-run launch intents.
5. Keep the old code, snapshot, and import mappings intact until parity and the observation window are accepted. Report the failed gate and the exact recovery evidence before attempting another switch.

No deploy, route change, queue drain, credential rotation, data deletion, or rollback is part of the present task.
