# Hub2 historical reconciliation fixture (canary 5)

ADDITIVE migration-mapping evidence for the `historical` backlog workstream
(160 members). Produced by the Hub-bypass canary session `hub2-canary-hist`
(2026-11-04) on worktree `agent-hub-wt-hub2-hist-1104`, base `09bde6ca`.

The Hub2 coordinator alone decides what to do with this; nothing here is a
rollout blocker and nothing was executed against the live Hub (its API was
down; the record state below is read-only evidence).

## What the 160 members are

- **6 members** are not loss recreations: the current Hub2 capability-gap
  intakes the relevance ledger classified `KEEP needed in Hub2`
  (admission_refused routing, no-hard-delete/restore AC, scope matcher,
  session-hold reasons, the cancelled no-AC-stub tails-triage umbrella,
  zcodex native-exit handling). Verdict `keep` with a suggested sibling
  workstream lane (`quota`, `records`, `execution`, or `historical`).
- **154 members** are recreations created after the 2026-10-26 Hub1 intake
  loss (273 originals verified 404 live; 284 recreations mapped by recovery
  intake 955e9381). Each carries the original id (full or prefix-only),
  per-snapshot status layers (10-29 / 10-31 / 11-01), the 2026-11-03
  pre-loss status, the 11-03 reopen result, triage evidence, and a verdict.

## Event chain (evidence, not inference)

1. 2026-10-26: prune hard-deleted Hub1 intakes; 273/273 prefixes 404.
2. 2026-10-26: 284 additive recreations (new ids, `[recreated, orig X]`
   titles, no AC, all open, `ORIGINAL FINAL STATE` notes where known).
3. 2026-10-29..11-03: drain/triage waves close most recreations
   (`backlog-triage-20261029.md`, drain TSVs).
4. 2026-11-03 01:56Z: second loss; originals still 404; recreations restored
   (pre-loss statuses recorded: 219 done / 37 cancelled / 28 open).
5. 2026-11-03 02:05-02:15Z: deliberate reopen wave: all 284 back to open,
   done context preserved as inline markers.
6. 2026-11-03 20:20Z: Hub disabled on Cloudflare (cost), hence the 404s.

## Verdicts (per member, with reasons in the fixture)

| Verdict | Count | Meaning |
| --- | --- | --- |
| `keep` | 6 | current-capability-gap; stays in Hub2 scope with a suggested lane |
| `skip` | 131 | finished (repo-evidence 69 / report-only 21) or superseded/duplicate (41); no Hub2 action, evidence attached |
| `reclassify` | 23 | triage names a non-Hub owner project; attach evidence there (`external:<project>`) |

Also flagged in `summary`: 4 original-id prefixes mapped to multiple
recreations (e.g. `3f9cbb65` -> two near-identical UUID variants) and the one
prefix-only original (`c0af9bc8` -> `4cd05ea7`).

## Files

- `build_hist_fixture.py` - stdlib-only deterministic builder; all 15 evidence
  inputs are sha256-pinned, any drift/missing file fails closed. Re-run only on
  the host that holds `/home/alexey/tmp` evidence.
- `brief-member-ids.txt` - the 160 exact-once member ids, extracted verbatim.
- `fixtures/hub2-hist-mapping-fixture.json` - the committed artifact.

## Coordinator import notes

- Additive only: no source record, id, event or export is mutated or deleted;
  import appends mapping/verdict data alongside the migration export per
  `docs/hub2/MIGRATION.md` (source ID/version + migration generation).
- Credential-bearing fields (actors, tokens) are stripped; fixture content is
  evidence provenance (paths, statuses, verdicts), not intake payload text.
- `skip` does not authorize closing anything: recovery intake 955e9381 says
  "Never close this intake" for recreations, and closure remains an explicit
  operator decision per record.
- Characterization tests: `host/tests/test_hub2_hist_fixture.py` (24 tests,
  self-contained, no evidence-input dependency).
