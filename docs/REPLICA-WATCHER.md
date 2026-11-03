# Grok Bot replica watcher (intake 290b935a)

Token-free feed of both sides of every Grok Bot chat into the Hub message
ledger ([ASK-LEDGER.md](ASK-LEDGER.md), intake 55c10deb). No Grok Bot turn,
routine or `ReadTranscript` call runs, so no Grok/Developer tokens are spent.

## Source

The Grok Bot desktop app keeps a plain-JSON rolling replica (about 200
entries) of every agent chat in
`%APPDATA%\Grok Bot\sand-client-persistence\*.blob`. A file stem is base32 of
`sand.client.slice.account.grok|<user>.transcript.replicas.<agentId>`; the
content is `{schemaVersion:1, value:{entries:[...]}}`. Entry ids are
`t<turn><role><n>`: `tNNNNu` is Alexey, `tNNNNsM` is the agent's M-th send in
that turn. The replica updates live, including for messages sent from the
phone, once the app has synced.

Only `*.transcript.replicas.*` blobs are opened. Each file name is decoded
first; no other blob (push state, webhook keys), Local Storage or secrets
file is ever opened. Logs carry counts only, never message text.
Attachments are sent as references (name, mime, size, ref), never content.

## What is captured (A12)

Every entry with a `t…` id except `event` entries: Alexey's messages,
widget answers and attachments (`role: user`), and every agent send
(`role: assistant`) for all agents (Developer, Orin, Tofu and any other).
`reply_to` is the explicit reply field when the replica has one, otherwise
an agent send points at the same turn's user message (`t1985s1` → `t1985u`).

## Runtime

`host/agent_hub_host/replica_watcher.py` runs as the `replica-watcher` daemon
thread of the session host. On Tabo that is the existing
`AgentHubSessionHost` Scheduled Task (AtStartup + AtLogOn, S4U, same user):
no admin, no UAC, no extra task. It is disabled where the replica directory
does not exist (every non-Windows host) or when
`<journal>/replica-watcher.json` holds `{"enabled": false}`. It ships with the
normal Hub-pinned host release (`hub host-release --machine <id> --sha <main
sha>`, [HOST-RELEASE.md](HOST-RELEASE.md)); moving it to another Windows
machine (A7) is enrolling that machine's session host. Two watchers can run
at once: the Hub deduplicates on `(agent_id, msg_id)`.

- Polls every 30 s; posts when a replica changed, otherwise a heartbeat every
  10 minutes. The Hub alerts once when no ingest arrived for more than 2 h
  while the machine's host heartbeat is fresh.
- **Catch-up (A8).** Controller start (boot, logon, restart) and a resume
  from sleep (wall clock jumped more than 2 min past the monotonic clock)
  post with `reason: startup|resume` and poll every 10 s for 10 minutes while
  the app syncs. `<journal>/replica-watcher-state.json` keeps, per agent, the
  message ids the Hub acknowledged; everything not acknowledged is sent, so
  messages written from the phone while the box was off are ingested on
  return. A first run backfills whatever the replicas hold.
- **Overflow (A9).** If the replica window starts after the last
  acknowledged seq (the window rotated past everything seen), the agent entry
  carries `gap: {after_msg_id, after_seq, first_seq_in_window}` and the Hub
  records it as a known gap. Gaps are never dropped silently.
- **Reconciler (A10).** The Hub starts the reconciler session after an
  ingest batch with new user messages (owned by the ledger side, 55c10deb).
- Posts are split to at most 200 messages per agent, 32 agents and 240 KiB per
  body, in replica order. Texts are never truncated. Errors back off up to
  10 minutes; the cursor only advances on an `ok` response.

## Ingest body

```json
{"source": "tabo-replica", "machine_id": "<this host>", "observed_at": 1793271800000, "reason": "poll",
 "agents": [{"agent_id": "e96e39a2-...", "agent_name": "Developer",
   "window": {"entry_count": 200, "first_msg_id": "t1790u", "last_msg_id": "t1985s1",
              "first_seq": 1790, "last_seq": 1985,
              "diag": {"user_entries": 61, "assistant_entries": 120, "without_text": 0, "without_ts": 0,
                       "kinds": {"message": 61, "send-message": 120}, "user_keys": ["id", "kind", "text", "ts"],
                       "assistant_keys": ["id", "kind", "text", "ts"]}},
   "gap": null,
   "messages": [{"msg_id": "t1985u", "t_id": "t1985", "seq": 1985, "role": "user", "kind": "message",
                 "ts": 1793271720000, "text": "<verbatim>", "reply_to": null, "attachments": []}]}]}
```

`window.diag` holds counts and field names only. A non-zero `without_text`
means the replica format changed and the extractor needs an update.

## JSONL record (A13)

One object per message, append-only, idempotent on `(agent_id, msg_id)`.
The watcher writes it locally to `<journal>/ledger.jsonl` (offline copy,
written before the Hub post); the Hub serves the same schema on its read
path (see [ASK-LEDGER.md](ASK-LEDGER.md)). Sample:
[host/tests/fixtures/replica-ledger-sample.jsonl](../host/tests/fixtures/replica-ledger-sample.jsonl).

| field | meaning |
|---|---|
| `agent_id`, `agent_name` | replica agent; name for known agents, else `null` |
| `msg_id` | entry id, e.g. `t1985u`, `t1985s1` (unique per agent) |
| `t_id` | turn id, e.g. `t1985` |
| `role` | `user`, `assistant` or `system` |
| `kind` | replica kind: `message`, `send-message`, `user-attachment`, `widget-answer`, … |
| `ts_utc`, `ts_berlin` | ISO 8601, seconds; `null` when the entry has no time |
| `text` | verbatim, never truncated; `null` for a bare attachment |
| `reply_to` | `msg_id` this answers, or `null` |
| `attachments` | `[{name, mime, size, ref}]`, references only |
| `source` | `tabo-replica` |
