# Intake capacity

`INTAKE_LIMITS.taskBytes` (128 KiB) bounds task metadata, excluding the
append-only `history` ledger. Notes, verified session links, assignment updates,
worktree receipts and service writes share this accounting. Existing records
need no migration: every original event remains in the same task row and reads
and exact replay receipts preserve it.

History still counts against the 128-event limit and the full hot storage budget
(128 MiB). Individual input requests remain bounded at 32 KiB with existing
field validation. Full task records have a separate 896 KiB safety limit. The SQL projection repeats link/report data in two
columns, so this leaves room for both within the
[2 MB SQL row limit](https://developers.cloudflare.com/durable-objects/platform/limits/).
Exact response receipts have a separate 1920 KiB budget for history, duplicated
unresolved events and their envelope. Other records retain the 256 KiB bound. These are
explicit storage safety limits, not unlimited history retention: reaching the
event, record or aggregate budget still refuses a write without changing the
existing ledger. There is no automatic deletion or compaction in this change.

This separates ordinary metadata admission from accumulated history so an open
intake such as the 173 KiB 95c1862c case can accept new notes and session links.
Both the prior 128 KiB admission cliff and the 256 KiB task/receipt cliff are
covered by regression tests. Metadata and aggregate storage failures continue
to roll back the task, receipt and revision in the surrounding transaction.
