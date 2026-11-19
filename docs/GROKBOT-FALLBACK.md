# GrokBot <1% fallback: quota export, sticky Desktop sessions, cross-machine mail, reliable Desktop link

Intake 408b585e (Alexey, 2026-11-19). Dapier watches Alexey's inbox and forwards
role emails (Orin/Tofu/Dev) to the GrokBot webhook by default. When GrokBot's
remaining quota drops below 1%, Dapier instead pings a sticky interactive Hub
session on the Desktop machine ("Hey, check your emails."). This document is the
Hub-side contract and runbook. The Dapier-side consumer is intake f28baca0.

## 1. Quota evidence export (AC1)

Sanitized remaining-quota evidence per account label, stored in ControlDO
(`quota_reports`, schema version 8):

- `GET /api/quota/<account>` — any authenticated Hub credential (human device
  login, session worker, machine). Response:

  ```json
  {"account":"grokbot","provider":"grok","remaining_percent":0.7,
   "threshold_percent":1,"below_threshold":true,"fresh":true,
   "state":"exhausted","observed_at":"2026-11-19T12:00:00Z",
   "reset_at":null,"source":"grok-replica","updated_at":1795076400000}
  ```

- The consumer decision rule is Hub-computed: `below_threshold = fresh &&
  remaining_percent < threshold_percent`. `state` is `fresh` (above threshold),
  `exhausted` (at/below threshold), `stale` (freshness window expired) or
  `unknown` (never reported). Stale and unknown evidence never report
  `below_threshold=true`: a consumer that acts only on
  `below_threshold===true` fails closed.
- `PUT /api/quota/<account>` with
  `{remaining, source, captured_at, valid_for_ms?, reset_at?, threshold?}`
  records evidence. Only `operator:admin` may write (the reporter is the
  operator-side Grok replica or an operator-run probe). Evidence is monotonic
  (older `captured_at` refused with 409), percentages are 0–100, and the
  freshness window defaults to 5 minutes (max 1 hour). Reused evidence is not
  replayable: `captured_at` must advance.
- CLI: `hub quota check grokbot [--json]` reads the export;
  `hub quota report grokbot --remaining 0.7 --source grok-replica` records it.
  The fleet projection `GET /api/quota?view=fleet` and `hub quota --json`
  remain the provider-account views and are unchanged (admin).

## 2. Sticky sessions tied to Desktop (AC2)

Named agents are the stable role identities: `agent:orin`, `agent:tofu`,
`agent:dev` (ControlDO `named_agents`). A sticky interactive Hub session on
`DESKTOP-FP4OP26` claims its agent; messages to the agent are then typed into
that session's TUI through the machine WebSocket (§4) and confirmed with
`delivered_at` + an aplexer native receipt. If no live sticky session exists,
mail stays durable in the agent mailbox and delivers when one claims again.

- Operator setup (admin credential):
  - `hub agents create orin --display Orin --machine DESKTOP-FP4OP26`
    registers the agent with a machine pin; `hub agents forward orin --to
    session:<uuid>` manually pins a session, `--clear` unsets it.
  - `hub agents sticky orin --family <family-id>` grants a credential family
    the right to claim the forward target; `--revoke` removes it.
- Sticky session startup (inside the Desktop session, using its own worker
  credential): `hub agents claim orin`. Without a grant the typed 403
  `{error:"forward_grant_required",family_id:"worker:<session>:<epoch>"}`
  names the exact family to grant. With a pin configured, only a session whose
  route owner is the pinned machine may claim; a mismatch answers
  `{error:"sticky_machine_mismatch",required_machine:"DESKTOP-FP4OP26"}`.
- `GET /api/named-agents` (admin) lists agents with `forward_session`,
  `machine` and forward grants; `hub agents show orin` shows one.
- Dapier delivery stays on `POST /api/messages` (`recipient: "agent:orin"`,
  idempotency `key`); `GET /api/messages/<id>/status` is the receipt.

## 3. Cross-machine message hub (AC3)

The Hub2 mailbox is already the cross-machine message hub: `hub msg send
--to session:<uuid>|human:<sub>|agent:<name>` from any enrolled machine lands
in the recipient mailbox, and session recipients receive native delivery into
their TUI via the host daemon regardless of which machine they run on
(Hetzner <-> Desktop included). Recipient resolution, receipts
(read/ack/accept/complete/delivered), threading and readdress are covered by
[SESSION-MESSAGING.md](SESSION-MESSAGING.md). Verification and the live
cross-machine proof for this intake are recorded on the tracking issue.

## 4. Reliable Hub-to-Desktop link (AC4)

Desktop needs no inbound connectivity. The enrolled host daemon
(`agent-hub2-host` / `hub2-host serve`) dials `wss://<hub>/api/connect`
(outbound, authenticated with the machine credential), heartbeats over HTTP,
and receives offers, commands and native mail pushed over that socket. On
(re)connect the Hub sends `resync` and re-flushes every pending command and
mailbox message; native mail that cannot be delivered retries with bounded
backoff and surfaces `attention` evidence instead of being dropped. A
heartbeat without a live socket opens a disconnect episode that alerts the
operator (`host_disconnected`). Reliability work is therefore deployment-side:
keep the daemon unit enabled on Desktop and monitor the disconnect alerts; no
inbound SSH or port forwarding is involved.
