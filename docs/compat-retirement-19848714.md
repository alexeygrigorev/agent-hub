# Persisted-data compat retirement — intake 19848714 (audit e2501fde C2)

Retired compat paths with read-only prod evidence, and dated plans for the
items that still have old-format rows. Evidence method: read-only census of
the daily Hub DO backup export (`/var/backups/hub-do`, export
`2026-10-28T03:21:29Z`), fresh count queries via the existing operator backup
endpoint (`/api/backup/status`, `/api/backup/export`, 2026-10-28T18:39Z), and
read-only host state file censuses on Hetzner. No prod data was deleted or
rewritten in this task. Full raw evidence: private `.local/19848714/`.

## Removed (zero remaining old-format rows/files)

| Intake item | Compat path | Evidence at removal |
|---|---|---|
| 4 | `agent_registry.ts` M3/M4 tag-PK rebuild | live DO `agents` DDL current; 462 rows, 0 projectless duplicate tags |
| 5c | `jobs.ts` legacy writer dated-key identity | 17 jobs rows, 0 keyed `writer-run:` without `payload.date` |
| 7a | `launcher_recovery.settle_retired_legacy` sweep | 0 unbound hub-fleet tasks in settle-eligible states (launcher binds at native start) |
| 7b | `guarded_root` uncleaned-finished legacy tolerance | 0 of 956 host state files legacy-shaped |
| 7c | `bridge.py` `acked_through` cursor reads | 0 of 1385 host cursor files carry it; aplexer only migrates it away, never emits |

Commits: df3735a0 (item 4), af79dcc3 (5c), c2a3da2d (7a), 3dd54d1b (7b),
ef37530c (7c).

Caveat carried by the removals: restoring a Hub DO backup older than the
M5 migration, or running a DO database older than `project`-column agents,
now fails closed (`unsupported_agents_schema` / missing data) instead of
silently rebuilding. Operator restores of such backups would need the
pre-2026-10 Worker build, which rollback.yml already pins.

## Dated plans (old-format rows remain; no data was rewritten here)

1. **urgent-muse → shared-go account alias** — `fleet_accounts` still holds an
   enabled `urgent-muse` row (provider `go`) and `codex-orin` provider label;
   942 historical `fleet_launches` rows carry account `urgent-muse` (newest
   2026-10-27); a stale `server_quota_snapshots` row (`urgent-muse`,
   captured 2026-10-26) remains. Plan (operator actions + follow-up code task,
   target ≤ 2026-11-15): stop producing legacy labels (verify no route/config
   still emits `urgent-muse` / provider `codex-orin`), then operator removes
   the legacy `fleet_accounts` rows and the stale snapshot via the existing
   engines CLI (a prod mutation — separate approved task), then remove
   `LEGACY_ACCOUNT_LABELS`, `LEGACY_QUOTA_ROUTE_PROVIDERS`, the
   `config/model-routing.json` `account_aliases` entry and their host-side
   counterparts (`model_contract.account_aliases`, `account_registry.py`,
   `quota_server.py`).
2. **approval_seq/approved_seq NULL tolerance** — machine `hetzner`
   (SnV2sxRq…) is pre-migration (approval_seq NULL, last check-in
   2026-10-15, dead since); its host credential `gKnwEmd0…` expires
   2026-11-14. The two live machines (urgent-hetzner, tabo) have seq 1; no
   approved pairing lacks `approved_seq`. Plan (target 2026-11-14, after the
   legacy credential's natural expiry): operator deletes the dead machine
   row (prod mutation, separate task), then drop the NULL tolerance in
   `host_enrollment.approvalAuthorityMatches` and the schema comments.
3. **status_baselines inline snapshots** — 16 of 20/21 baseline rows still
   hold inline snapshots (callers that have not polled since the payload
   split; newest update 2026-10-26). Migration is lazy per caller poll.
   Plan (check 2026-11-30, or earlier once a fresh count shows zero): remove
   the inline read branch in `status.ts`. Note: a rolled-back older Worker
   writes inline baselines again; removal must land together with the
   rollback-pin note so post-rollback diffs degrade to "all tasks changed"
   (benign baseline reset) instead of silently re-reading inline rows.
4. **session rows: request_json without parent_id/intake_task_id** — 1310 of
   3672 sessions (backup census) predate lineage fields; idempotency replay
   backfills defaults. Sessions have no retention, so rows persist. Plan
   (follow-up migration task, target ≤ 2026-11-30): one-time backfill of the
   missing keys into stored `request_json` (explicit data migration with
   acknowledgment), then remove the hasOwnProperty backfill and the
   order-insensitive `norm` comparison keeps only its canonical form.
5. **legacy terminal receipts without diagnostics** — 250 failed sessions /
   1320 exited-failed launches lack `launch_error_json` (pre-diagnostic DTO
   rows). They age only with session retention (none today). Plan: fold into
   the same follow-up migration task as item 4 (≤ 2026-11-30): either
   backfill bounded synthesized diagnostics or add dated session retention;
   then remove the synthesized-reason fallback in `session_control.ts`.
6. **host history legacy observation sources** — 22 995 observations in
   `resource_history` packets still use sources `fleet_account_status` /
   `fleet_catalog_and_launches` (alias gauges). Retention (32-day history
   window) ages the last pre-rename buckets out by ≈ 2026-11-25. Plan
   (check on/after 2026-11-25 with a fresh count = 0): remove the
   `legacyNative` / `legacyRunning` provenance downgrade in
   `host-history.ts`.
7. **token_hours_posted plain-entity ledger** — 53 rows keyed by bare
   `entity` (pre machine-scoped ledger); machine-tagged rows are now flowing
   (289 rows, newest 2026-10-28T17:00Z) and no new plain rows appeared after
   the cutover. The 32-day token retention deletes the last plain rows ≈
   2026-11-29. Plan (check on/after 2026-11-29 with a fresh count = 0):
   remove the legacy same-hour revision seed in `token-history.ts`.

## Out of scope (ownership boundary)

- `bridge.py` cursor **format** itself is aplexer-owned (scope policy v2:
  session runtime internals). Hub stopped reading/emitting the retired
  field; any change to what aplexer writes stays with aplexer.
- Prod data mutations (deleting the dead `hetzner` machine, removing legacy
  `fleet_accounts` rows) are explicitly excluded from intake 19848714
  ("never delete or rewrite data in this task") and belong to the follow-up
  operator tasks above. Never approve prod gates as part of them.
