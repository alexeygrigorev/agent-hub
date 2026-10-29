# Intake capacity and retained notes

Open intakes automatically move ordinary old notes into an append-only SQLite
archive when inline history exceeds 128 KiB or needs another event slot beyond
128 events. The original intake, structured events and control notes (DONE, ORIGINAL FINAL STATE, START/STARTED, triage verdicts and
verification outcomes) remain inline, preserving completion and
obligation semantics. Compaction targets 75% of the inline limits and occurs
in the same transaction as the accepted mutation. A failed mutation rolls back
both archive and task writes; no note body, identity, provenance or retry key
is deleted or rewritten. Archives have no automatic expiry.

Task responses include `history_archive.count` and an authenticated `href` when
inline history is partial. Read originals with:

```sh
hub intake history TASK --archived --limit 100
hub intake history TASK --archived --after SEQUENCE --limit 100
```

Pages are bounded by event count and encoded bytes. Each page returns original
events with stable sequence numbers, a `next_after`
cursor, and `inline_sequences` for merging the retained inline events in their
original order. Every page requires the same current task read permission;
the pointer grants no access. Archives count toward retained storage accounting
and its existing planning warning, separately from hot task admission.

A `capacity_warning` is included in task reads and mutation results at 80% of
the inline byte or event target, before a hard capacity refusal. Ordinary notes
are retained automatically. Structured-event exhaustion, malformed legacy
identities or exhausted aggregate storage can still refuse a write explicitly,
with the original ledger intact.

Mutable metadata remains bounded at 128 KiB. Full task records retain an
896 KiB safety bound, accounting for duplicated SQL projection columns within
the [2 MB SQL row limit](https://developers.cloudflare.com/durable-objects/platform/limits/).
Exact response receipts allow 1920 KiB. Request/field validation and the
128 MiB aggregate hot budget remain enforced. Session links, public appends,
assignment updates, worktree events and service notes use this retention path.
