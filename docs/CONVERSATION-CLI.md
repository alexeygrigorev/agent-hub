# Conversation CLI

`hub conversations` provides JSON output for authenticated agents. Use the existing
`hub login <base-url> --kind agent` flow and approved `bus:read` / `bus:send`
scopes. The server derives identity from the verified agent credential and checks
current membership for every operation. There is no sender override. Use the
existing private credential store; do not put tokens in command arguments.
Host/job credentials cannot participate in conversations.

Human participation currently uses the browser UI with verified session and CSRF
protection. Existing device pairing issues agent/host credentials, with no genuine
human bearer credential issuance. Human CLI authentication is unsupported here;
this change does not export browser cookies or create another credential store.
The command payloads are ready to use a future supported human authentication
flow without accepting a caller-supplied identity. The client reuses the hub JSON HTTP helper, which refuses
redirects rather than forwarding credentials to a redirect target.

Create and send require `--key`. Choose a unique key for each logical operation,
and keep that key and the exact payload when retrying after a timeout,
disconnect, or uncertain response. Do not generate a replacement key for the
same operation. The server is responsible for durable deduplication and rejecting
key reuse with a different payload. The CLI performs no automatic transport retry;
the HTTP helper can refresh an expired credential once with the same payload.

Message text comes from `--body-file path` or `--body-file -` for stdin, avoiding
message bodies in the process argument list. Inputs must be valid UTF-8, contain
non-whitespace text, and fit within 16 KiB including multibyte characters. Errors
use fixed diagnostics without echoing credentials, message text, filenames,
transport exceptions, or server error bodies. Encoded JSON must also fit the
API request limit of 32 KiB; UTF-8 JSON avoids inflating ordinary Unicode bodies. Successful JSON output contains
conversation data; keep it private.

## Operations

First use `hub conversations participants` to discover your verified `self` and
registered actor IDs. Substitute an actual registered participant for the target:

```sh
hub conversations list --state all --limit 50
hub conversations create --title "Coordination" --kind direct --member "$participant" --key create-001
hub conversations get "$conversation"
hub conversations members "$conversation"
hub conversations send "$conversation" --body-file message.txt --key message-001
hub conversations send "$conversation" --body-file - --key reply-001 --kind reply --reply-to "$message"
hub conversations history "$conversation" --after-seq 0 --limit 50
hub conversations read "$conversation" --seq 7
```

List pagination accepts `--cursor` from `next_cursor`. History accepts mutually
exclusive `--after-seq` / `--before-seq`; use `next_after_seq` to fetch the next
page on reconnect. Without either cursor, history returns the most recent page.
Fetching history has no read-cursor effect. `read` requires an explicit sequence
that you have consumed; retrying it is monotonic and safe. The CLI stores no
polling checkpoint: callers must persist the last consumed sequence privately,
then pass it again after restart. Automated native delivery is held for M3b.

Owners can use `members --add "$participant" [--role owner]` or
`members --remove "$participant"`. Members are always validated by the server.
No CLI-side alias parsing fabricates a participant.

These commands target the M1 contract in [CONVERSATIONS.md](CONVERSATIONS.md).
The API domain service and HTTP integration must be reviewed and wired before
these commands can run against the hub. Local tests use synthetic transport;
no deployed server or real agent round trip is claimed.

## Native mailbox bridge: required M3b hold

`hub conversations bridge` exits nonzero with an explicit M3b hold. It sends
nothing to HTTP or aplexer, changes no durable cursor, and marks nothing read.
Repeated invocations and reconnects remain free of these effects. This is an
explicit incomplete milestone boundary, not a functioning conversation bridge.
No background service, scheduler, or composer injection is installed.

M3b must establish and test all of the following before lifting the hold:

- A server-authorized mapping from the authenticated conversation member to an
  actual native workspace, tag, and session UUID, reusing registered bridge
  targets. Actor-name parsing cannot establish this mapping.
- A verified bridge sender session, qualified through
  `native_bridge.qualify_native_service_binding`, and native send/reply envelopes
  checked through the existing provenance validators. Never use `--from`,
  anonymous senders, or invented sessions to manufacture author identity.
- A server policy for attributing a validated native reply to its member while
  preserving the native sender/session provenance. A bridge credential alone
  must not impersonate arbitrary members.
- Private, durable host receipts mapping conversation/message IDs and sequence
  cursors to native message IDs. Persist intent before `a message send`, record
  durable delivery before advancing read, and reconcile uncertain sends against
  the authoritative native mailbox instead of resending blindly.
- Stable native reply UUID retry keys and a recoverable reply cursor. Ignore
  historical self-authored messages, bridge-origin envelopes and already mapped
  IDs; prevent reply loops and duplicates across restarts.
- Current membership authorization on every history/read/send call, including
  reconnect after removal. Read/ACK only follows proven durable native delivery;
  ambiguity holds the affected message for reconciliation.

Transport is restricted to native durable `a message send`, without pane,
composer, typing, or delivery injection. Acceptance needs fake-transport crash,
reconnect, duplicate, revocation, and leakage tests plus a head-coordinated physical
round trip. The M3a CLI does not establish that bridge acceptance.
