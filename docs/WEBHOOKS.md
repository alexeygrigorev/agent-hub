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

## Hub2 draft-job routes (AISL recording preparation)

The Hub2 adapter restores the same `/api/webhooks` management surface and
`/webhooks/<route id>` producer endpoint. It imports the original
`WebhookService`: token verification, request bounds, durable rate buckets,
rendering, audit and `session_deliveries` idempotency remain the same owners.
Control schema migration 9 adds those tables and job launch metadata. A host
running with schema migration `defer` returns `webhook_schema_not_ready` until
the migration is applied; it must not accept a receipt it cannot persist.

Hub2 currently supports token-authenticated draft-job routes. Old Hub1 session
labels, HMAC routes and prompt-library bindings are not silently migrated.
Create a new route with an operator credential and a fixed launch JSON file:

```json
{"project":"AI-Shipping-Labs/website","workspace":"/home/alexey/git/ai-shipping-labs-workshops-raw","machine":"hetzner-aplexer"}
```

```bash
hub webhooks create --name aisl-workshop-preparation \
  --launch-file /private/path/aisl-launch.json \
  --template-file ops/workshop-preparation.prompt.md \
  --secret-file /private/path/aisl-webhook.secret
```

Keep the resulting route URL and token in AISL's `RECORDING_PREPARATION_HUB_URL`
and `RECORDING_PREPARATION_HUB_TOKEN` integration settings (confirm current key
names against the website settings registry). Route config is operator-only.
The producer cannot supply a project, machine, credential, model or launch
parameter; only the operator-authored template renders its source fields.
Draft jobs use Opus with high effort, as required by
`workshop-followup-materials`. Missing quota or host availability remains a
durable pending delivery and retries every minute, at most five attempts.
AISL monitors the receipt independently and raises existing staff alerts.

For each delivery the adapter calls Hub2's existing session-start saga with
one stable start key derived from the durable delivery UUID. Retries and host
restarts reuse that intake/session. Receiving a webhook does not mean the job
started: `pending` is queued in the webhook, `queued` means Hub accepted a
session start, and `running` requires the native-start evidence in intake
stats. `finished` requires the intake's independently verified `accepted`
state, never process exit or a worker's completion claim alone.

Producers use the same route token to read:

```text
GET /webhooks/<route id>/deliveries/<delivery id>
X-Webhook-Token: <route secret>
```

The response contains `delivery_id`, `session_id`, `status`, `started_at`,
`finished_at`, `artifacts` and `last_error`. Times are UTC ISO 8601. A route
cannot read another route's receipt. No prompt, transcript or secret is
returned. An unavailable observation returns 503 instead of inventing state.

Job outputs stay under a unique `.local/` directory in the raw-workshops
checkout. The job appends a `recording_preparation_drafts` evidence record to
its own intake, containing the delivery UUID plus a two-output manifest:
`video_chapters` and `workshops_raw_summary`, each with a relative `.local/`
`path` and SHA256. The normal Hub acceptance verifier checks those files before
accepting the task. The producer requires both output references and start /
finish timestamps; a bare successful process is insufficient. Never embed a
private S3 URL or staff credential in output, audit or the manifest.

Operational bounds: Stage 1 is 60 minutes from recording availability to S3
upload and the host email's Relay delivery confirmation. Stage 2 starts within
5 minutes of transcript availability and finishes within 60 minutes. Website
watchdogs evaluate local deadlines every minute with four minutes of observation grace, independently of provider
network calls; reconciliation and alert observation have a documented maximum
5-minute scheduler tolerance. Missing integration configuration, refused
starts, stalled uploads, unsuccessful mail, and unfinished jobs all alert the
existing staff operations channel. These are monitored objectives, not a
promise that unavailable external providers finish successfully within them.

Deploy website support before publishing the Dapier workflow or configuring
the route. Provision the website API token privately in the job's runtime so
`asl` can fetch `/api/events/<slug>/preparation-source`. Validate with a
synthetic source in a separate test route; never replay an actual recording
into live operator mail or trigger attendee publication as a smoke test.
