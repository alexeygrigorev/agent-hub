# Explicit conversation bridge history recovery

The bridge holds when its next message is not `local_cursor + 1`. Retention can remove a prefix or an interior message; for example, retained sequences `1,3` with local cursor `1` leave sequence `2` unavailable. Default bridge cycles never skip that sequence. Gap metadata from the API is advisory and can miss interior gaps.

An operator who accepts that the missing history cannot be delivered can run:

```sh
hub bridge-resume --enable-native --acknowledge-gap \
  --actor project-label:worker-tag --project project-label \
  --workspace /absolute/canonical/project/workspace \
  --machine-id registered-machine --conversation conversation-id \
  --resume-after-seq 2
```

Use the same existing configuration, credential, project and genuine native session as `hub conversations bridge`. This top-level command leaves the reviewed `conversations.py` client unchanged. It accepts exactly one conversation and requires an existing journal. `--resume-after-seq` is the explicit new **local** cursor; it must be an integer greater than the saved cursor and no greater than the authenticated server's current `last_seq`. Acknowledgement authorizes only a range that contains no retained messages.

For retained `1,3` and cursor `1`, target `2` acknowledges missing range `[2,2]`. Target `3` is refused because it would skip a retained message. For retained `3` and cursor `0`, target `2` acknowledges `[1,2]`. A fully pruned tail can also be acknowledged when the canonical page proves emptiness and the target is within `last_seq`.

## Evidence and effects

Recovery requalifies the participant's own UUID, tag and canonical workspace using the installed native CLI and authoritative session records. The credential must currently authenticate that registered project/actor/machine, have conversation access scope, and belong to the conversation. Human, host, job, foreign-actor and delegated identities cannot substitute for this binding. Server authentication, scope and membership checks precede local lock creation. They are repeated under the existing nonblocking journal flock and after history verification.

Under that lock, the command reloads the exact immutable v1 journal identity and validates its cursor, deliveries, reply intents and prior gap records. Existing v1 journals without a `gaps` field remain valid. Unknown extension fields are preserved. Malformed records, duplicate JSON fields, non-finite values, unsupported versions, unsafe files and oversized journals fail closed. Recovery refuses every unresolved native delivery, pending read or unfinished reply intent across the journal. It never clears or resends them. Use the ordinary bridge's existing receipt reconciliation where possible; pruned originals or missing native receipts can still require operator reconciliation.

The proof uses an explicit ascending `messages?after_seq=LOCAL_CURSOR&limit=100` page, then repeats it after another authority check. It validates actual rows, actor/conversation identity, strict sequence ordering, page size, retained bounds, page cursors, completeness flags and current watermarks. If the first retained row is inside the requested range, recovery is refused immediately, including targets beyond a page boundary. An empty page must agree with retained bounds and pagination. Missing fields or inconsistent watermarks are refused. The command never follows `resume_after_seq`, `gap_detected` or a default/latest history page to infer permission.

This depends on the canonical server contract: explicit `after_seq` returns the earliest retained rows in ascending sequence order, and public sends allocate monotonically increasing sequences. Public sends cannot insert an older sequence into the verified interval. Client checks cannot attest an arbitrary server implementation that fabricates or omits rows. Final membership checks fence observed changes; membership and local filesystem commits cannot be atomic across systems.

The gap record and local cursor are committed together by exclusive temporary file, file `fsync`, atomic replacement and directory `fsync`. Each appended record contains the missing range, old/new cursor, explicit acknowledgement, fixed reason, verification time, full verified origin/actor/project/workspace/machine/UUID/tag identity, server `last_seq` and server read watermark. Recovery issues no native send, reply POST or read POST. Missing messages acquire no delivery mappings or read receipts. Output reports `status: gap_recorded`, `server_read_advanced: false` and a visible missing-history notice.

After successful acknowledgement, run the normal bridge with the same mapping. Only retained messages beyond the new cursor are delivered and then receive ordinary authenticated read receipts. A later server read watermark can therefore span an acknowledged missing range; the journal distinguishes that acknowledgement from actual deliveries.

## Restart, limits and refusal

Equal or backward targets always fail, including exact replay after success. The command still checks current authority before refusing a replay. If the process stops after replacement but before reporting success, inspect the private journal: a persisted gap plus its new cursor means the acknowledgement committed. Do not delete the journal, change project, edit identity/cursors, clear attempted reservations or restart with fresh state to retry. A crash before replacement leaves the old journal; an abandoned `.tmp` file requires inspection and holds further writes. Concurrent normal and recovery processes use the same flock.

Gap history is append-only and bounded to 128 records within the existing 8 MiB total journal limit. At either limit recovery holds and retains all evidence; no audit, delivery or uncertainty record is evicted. Each attempt makes at most 12 authenticated GET calls, including three bounded history requests (one initial scope check with limit 1, two proof pages with limit 100). Proof pages have a 2 MiB validated JSON bound; the unchanged HTTP client also caps each raw response at 10 MiB. There is no pagination walk, broad backfill or automatic skip-all operation. State replacement by another trusted local process, or rollback of an entire valid journal together with all its audit evidence, needs external operator custody; this local journal is not an external anti-rollback authority.

## Local verification

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=host python3 -m unittest discover -s host/tests -p 'test_conversation_bridge*.py'
TMPDIR="$PWD/.local/tmp" UV_THREADPOOL_SIZE=1 NODE_OPTIONS=--v8-pool-size=1 \
  GOMAXPROCS=1 taskset -c 0 node test/conversation-bridge-gap.mjs
```

The HTTP fixture reconstructs immutable integration source `d9b4cde07e3718d77b2db9308cd323a5c8e003ed` under ignored `.local/bridge-gap/`, preserving the real Worker, SQLite schema and constraints. It copies only required source/config files and adds a local fixture entrypoint; maintained `src/` is never imported or edited. Existing dependencies are linked, never installed. Public registration and conversation routes create fixture actors and messages; local-only seed credentials and expiry hooks are unrelated to production enrollment.

The `--native` variant is intentionally pinned to the original implementer's genuine UUID/tag/workspace and the head-approved `gap-local` journal paths. It refuses an existing journal; do not remove that journal or create a new project to rerun it. An independent reviewer needs their own genuine session and explicit journal-scope approval before adapting the fixture. It sends nonce-only inbox content to the caller itself, never pane input or borrowed identities. Private evidence remains under `.local/bridge-gap/` and the approved journal.

These checks establish local source behavior. Independent exact-version review, accepted M1/F7 integration, physical multi-host verification and native idempotent-send recovery remain separate gates.
