# Per-participant native conversation inbox bridge (M3b)

The bridge is a bounded foreground client for **one enrolled agent using its own existing agent credential inside its own genuine aplexer session**. It delivers conversation content to that session's durable mailbox and relays that session's native replies through the same authenticated HTTP client. Two independently enrolled clients can participate in a human/agent conversation. No host delegation or author override is involved.

This candidate builds on the M1 conversation contract and M3a CLI candidate `0a06f5e`. M1 routes still require head integration and independent review. Local tests and a native nonce probe are not deployed hub or cross-host acceptance.

## Run explicitly

Use the existing safe host configuration and credential store (`HUB_CONFIG_FILE`, when needed, must name the existing absolute path). The existing config's authenticated `machine_id` must match `--machine-id`. Existing agent bearer environment support also remains available; do not put a token on a command line or copy credentials into bridge files. Human, job and host credentials cannot satisfy the server's agent identity contract.

Run from the participant's actual native session:

```sh
hub conversations bridge --enable-native --once \
  --actor project-label:worker-tag --project project-label \
  --workspace /absolute/canonical/project/workspace \
  --machine-id registered-machine --conversation conversation-id
```

Repeat `--conversation` to opt into up to 32 conversations. `--cycles N` accepts 1..1000 and runs immediately in the foreground. Each cycle processes one page of up to 100 messages per configured conversation, then proven native replies. There is no daemon, timer, global install or service change. Omission of `--enable-native` fails before transport or journal effects. This implementation uses Unix `flock` and directory `fsync`; unsupported hosts fail closed.

The project label is explicit and is never inferred from the workspace basename or a shared tag. Projectless registrations are currently unsupported. The canonical authenticated actor must equal `<project>:<native-tag>`. Registration must agree on project, tag and machine; contradictory optional workspace/session hints are refused. Static registration hints never establish live binding: the client separately checks genuine `whoami`, an unambiguous current catalog entry for that workspace/tag, and the existing authoritative native session-record qualifier. A session UUID change fails closed against the persisted journal.

The native sender remains the actual participant. The JSON envelope body contains the conversation ID, message ID, sequence, original `sender_actor` and body. These are conversation content, not native sender provenance or instructions from the operator. Native `from` metadata is never rewritten. Delivery uses `message send --workspace PATH --to TAG --kind hub-conversation --data JSON`, without `--pane`, `--queue`, `--from`, input, Enter, environment rebinding or UUID recipient routing. The recipient may inspect the durable inbox and reply using:

```sh
a message show NATIVE_ENVELOPE_ID --json
a message reply NATIVE_ENVELOPE_ID 'participant reply'
```

Only `kind=reply` messages referencing a persisted delivered envelope qualify for relay. The reply must have the same genuine native sender UUID/tag/workspace, exact own recipient UUID/tag, and inbox delivery. Reply text is sent as the existing credential's authenticated actor; `reply_to` and conversation are derived from the original delivery mapping. Unrelated mail, foreign authors, bridge deliveries and self-authored hub messages never generate relay loops. The client does not ACK native messages or mutate native ACK cursors; mailbox read policy stays with the participant.

## Delivery, restart and read semantics

A private journal under `<workspace>/.local/conversation-bridge-<actor/project-hash>.json` records origin, actor, project, workspace, machine, native UUID/tag, delivery payloads, native envelope mappings, reply outbox payloads and per-conversation read positions. It contains private conversation content but **no credentials**; do not publish it. Journal files use mode 0600; symlinks/unsafe ancestors, foreign-owned or group/world-readable journals are refused. An exclusive nonblocking cycle lock prevents competing clients from sending the same pending intent. State writes use exclusive temporary files, `fsync`, atomic replacement and directory `fsync`. Malformed or oversized journals fail closed (8 MiB bound); no receipt pruning discards deduplication evidence.

Before sending, the client persists both the payload and an attempted reservation. A deterministic per-message reference is included in native `data`. An authoritative native workspace log query must then prove exactly one matching own inbox envelope, including sender/recipient metadata, body, kind and reference. A CLI success alone never advances reads. An error after a durable native effect can be recovered immediately or on restart by the log query. An attempted send with no matching envelope remains uncertain and is **never resent**. Duplicate effects, changed payloads and contradictory mappings require reconciliation.

History starts at sequence zero for a new journal, including already read history, and is consumed in contiguous ascending order. Remote existing read positions are not used to skip messages that this client has not durably delivered. The bridge checks current authenticated membership before every native send, HTTP reply and read effect. Only after a proven native delivery (or recognition of a self-authored hub message) may it POST that explicit read sequence. The local cursor advances only after the server's monotonic read receipt. A crash or uncertain HTTP read repeats the same read update and reconciles the existing delivery; it does not resend native content. A remote membership change racing between the last check and a local send cannot be made atomic across these two systems; the next check fails closed, and the server independently enforces membership on each HTTP mutation.

Native reply outbox intent is saved before HTTP send. The hub key is deterministic over the pinned client identity and native reply ID. Retries reuse the exact payload/key and require the returned authenticated author, conversation, reply target and reference to agree. Pending outbox intents retry even if native log retention has removed the original reply; provenance and mapping were validated before intent persistence. A conflict retains the intent and fails closed. No author is inferred from reply body text.

## Recovery and remaining native limitation

Retention gaps hold by default. An operator can use the explicit `hub bridge-resume --enable-native --acknowledge-gap --resume-after-seq N` command with the same actor/project/workspace/machine mapping and one conversation. It proves that the acknowledged range contains no retained rows, then atomically records the gap and advances only the local cursor. It never marks missing messages delivered/read, resets state or bypasses uncertain intents. See [history recovery](CONVERSATION-BRIDGE-GAPS.md) for the complete command, proof, limits and replay semantics.

Retain journals and raw native receipts when an effect is uncertain. Do not delete an attempted reservation, edit UUID bindings, remove lock files under an active process, or restart with a fresh journal to force a retry. Logs can prove a late durable effect; absence from a retained log cannot prove that an attempted effect never happened. Lost or corrupt state and session rollover therefore need explicit operator reconciliation. This candidate intentionally provides no automated destructive reset.

For automatic recovery of a send that failed before durable persistence, the minimal upstream improvement is a native **idempotent inbox-send** primitive: accept a caller retry key scoped to genuine sender UUID/workspace and recipient UUID, atomically store the payload/envelope with that key, return the original envelope on exact retry, and reject conflicting payloads. This is not delegated-token or UUID-route support. The installed CLI does not offer that guarantee; current recovery safely stops instead of blind resend. Native log retention can similarly prevent automatic reconciliation; retain the private journal.

## Validation evidence

`PYTHONPATH=host python3 -m unittest discover -s host/tests` runs the host suite. Bridge tests exercise independent clients with same tags in distinct projects, author/machine/native UUID mismatches, membership removal, unrelated mail and reply mapping, reconnect deduplication, uncertain native effects with and without durable envelopes, late log recovery, uncertain HTTP reply/read results, conflicting retry payloads, contiguous cursor advancement, exclusive locks, private journal modes, unsafe symlinks and inbox-only CLI arguments.

On 2026-10-17, platform-bridge captured genuine `whoami` before effects and performed explicitly authorized nonce-only self-send and self-reply probes using the installed native CLI. The actual adapter also qualified native session records and verified structured `data` in the authoritative log. Sender and recipient UUIDs matched the genuine session and replies preserved `reply_to`; delivery was inbox-only. Raw receipts are private in `.local/native-probe/` and are not committed. These probes establish the installed self-mailbox primitive only. Full hub runtime, multiple real agent clients and cross-host acceptance remain for head-coordinated validation after integration.
