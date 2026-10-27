# Hub webhooks → session delivery

Intake `19ec91c2-3922-465b-8835-9ad9ab003b32` (AC1-AC7). The Hub exposes
authenticated public webhook endpoints that deliver an incoming payload as a
prompt message into a target Hub session, reusing the verified session-control
send path (durable commands + ack ledger) — no second transport.

- Ingest URL: `POST https://hub.dtcdev.click/webhooks/<route id>`
- Management: `hub webhooks …` (operator credential) or the dashboard page
  `/webhooks.html` (admin sign-in).
- Code: `src/webhooks.ts` (service + schema), `host/agent_hub_host/webhooks.py`
  (CLI), `public/webhooks.{html,js,css}` (UI).

## Route config (AC1)

A route maps its id to a target session plus an optional template:

| Field | Meaning |
| --- | --- |
| `name` | Unique lowercase slug (2-64 chars), used to address the route in the CLI. |
| `target_label` | Resolved at delivery time: exact Hub session UUID, exact friendly session name, or qualified native alias `<workspace basename>:<tag>`. Zero matches stay queued (the session may exist later); ambiguity fails the delivery permanently. |
| `template` | Optional render template (≤ 4096 chars). Placeholders: `{{payload}}` raw body, `{{payload_json}}` pretty JSON, `{{payload.<dot.path>}}` extracted field (arrays via indexes). Unknown placeholders render empty and are reported in the audit. Default: raw payload. Rendered text is truncated at 16 000 chars with an explicit marker (the session send limit). |

Create:

```bash
hub webhooks create --name oncall-dev --session oncall-engineer \
  --template 'dev@ alert: {{payload.subject}}
{{payload.body}}' \
  --secret-file ~/.config/agent-hub/webhooks/oncall-dev.secret
```

The secret is returned **exactly once** in the create response. The CLI writes
it to the given file with mode 0600 (`--secret-file`) or prints it to stdout
only with an explicit `--stdout-secret`. With neither flag the secret is
discarded and the route must be deleted and recreated. Routes are stored as
SHA-256 hashes (token mode) or XOR-wrapped with a key derived from the server
session secret (HMAC mode); list/show/UI never show secret material.

## Delivery semantics (AC2)

1. An accepted request becomes a durable `session_deliveries` row (`pending`).
2. The delivery pump (DO alarm, ≤ 60 s cadence, also on the 5-minute cron and
   inline after ingest) resolves the target session and, when it is bound and
   running, enqueues a session-control `send` command (status `queued`).
3. The owning host transports the command via aplexer and acks; the delivery
   follows the ack ledger (`delivered` only on a validated delivered proof).
4. While the target is missing or unbound the delivery stays pending and
   redelivers with bounded backoff (1→15 min) for ~24 h; transport/refusal
   failures retry 5 times; then the delivery `failed` with the last error.
   A returning session is picked up on the next pump.
5. Idempotency: send `X-Delivery-Id` (or `Idempotency-Key`) to make retries
   attach to the original delivery (replay returns the stored receipt and
   never duplicates). Send-command keys are attempt-scoped, so hub redeliveries
   and host transport retries cannot double-send into the session.

Statuses: `pending → queued → delivered` or `failed` (visible in the CLI
`hub webhooks deliveries <route>` and the UI).

## Authentication, limits (AC4)

- **token** (default): send the secret as `X-Webhook-Token: whsec_…`.
- **hmac**: send `X-Webhook-Timestamp` (unix seconds, ±5 min) and
  `X-Webhook-Signature: sha256=<hex>` where `<hex>` is
  `HMAC-SHA256(secret, "<timestamp>.<raw body>")`:

  ```bash
  ts=$(date +%s); body='{"subject":"disk 90%"}'
  sig=$(printf '%s.%s' "$ts" "$body" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
  curl -sS -X POST "https://hub.dtcdev.click/webhooks/$ROUTE_ID" \
    -H "X-Webhook-Timestamp: $ts" -H "X-Webhook-Signature: sha256=$sig" \
    --data-binary "$body"
  ```

- Rate limit: per-route fixed window, default 30 req/min (1-600); excess
  requests get `429` + `Retry-After`. The limit applies before authentication,
  so floods cannot turn the worker into free verification work. Trade-off:
  anyone holding the route URL can exhaust the route's own budget — treat
  `/webhooks/<route id>` as a capability URL and keep it confidential (share
  per producer, never embed in client-side code; prefer HMAC for producers
  you do not fully control).
- Payload size cap: default 64 KiB (1 KiB - 256 KiB); oversize → `413`.
- Audit: route create/delete/receive/test/deliver/fail are written to the Hub
  `audit` table with ids and statuses — never payload bodies or secrets.

## CLI (AC3)

```
hub webhooks create --name <slug> --session <label> [--template …] [--auth token|hmac]
                    [--rate-limit N] [--max-body-bytes N] [--disabled]
                    (--secret-file PATH | --stdout-secret)
hub webhooks list | show <id|name> | deliveries <id|name> [--limit N]
hub webhooks delete <id|name> --yes
hub webhooks test <id|name> [--payload TEXT | --payload-file F | --payload-json JSON]
                  [--delivery-key KEY] [--wait]
```

`list`/`show`/`deliveries` never print secrets. `test` injects a payload
through the same queue and render path (source `test`) and with `--wait` polls
until the delivery is delivered/failed — this is the AC6 live-proof tool.

## UI

`/webhooks.html`: route table (name, target, auth, rate, size, enabled),
create form (secret shown once), delete, per-route recent deliveries with
status, and the recent audit entries. Admin session required.

## Live proof (AC6)

The Dapier dev-mailbox route targets `oncall-engineer` (not
orchestrator-main). Until Dapier's webhook is repointed (do **not** reconfigure
the live Dapier→Grok Bot webhook without leaving the old wake working; the old
webhook is deleted only with Alexey's OK), prove end to end with
`hub webhooks test` and record the delivery + session evidence on the intake.

Runbook pointer: [ops/orchestrator/README.md](../ops/orchestrator/README.md)
(orchestrator-main / oncall-engineer operations). Scheduled prompts on the
same delivery queue (intake AC8-AC10) are specified in the intake and will be
documented here when they ship.
