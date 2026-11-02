# Hub prompt library and session schedules

Intake `19ec91c2` AC8-AC10. Engine-agnostic replacement for Claude `/loop`.

## Prompt library (`hub prompts`)

Versioned prompts addressable by id, slug, or `slug@N` (pin a version).
Every `update` appends an immutable version; `show` prints the current body.

```
hub prompts create --slug orchestrator-hourly --body-file hourly.md --title "Orchestrator hourly"
hub prompts list
hub prompts show orchestrator-hourly            # current body; --versions for history
hub prompts update orchestrator-hourly --body-file hourly.md --note "why"
hub prompts render oncall-triage --var team=dev # dry run, no delivery
hub prompts delete oncall-triage --yes          # refused while a schedule/webhook uses it
```

Bodies may use `{{var.NAME}}`; webhook routes additionally get `{{payload}}`,
`{{payload_json}}` and `{{payload.field}}`. Unknown placeholders render empty
with a warning, never raw markup.

## Session schedules (`hub schedules`)

Not `hub schedule` (singular), which launches a fresh headless session per tick.

```
hub schedules create --name orchestrator-hourly --session orchestrator-main \
  --cron '0 * * * *' --tz UTC --prompt-id orchestrator-hourly
hub schedules create --name x --session s --cron '*/15 * * * *' --prompt 'inline text'
hub schedules create --name y --session s --cron '0 9 * * 1-5' --prompt-file f.md
hub schedules list | show <name> | pause <name> | resume <name> | deliveries <name>
hub schedules fire <name> [--delivery-key K]    # one delivery now (idempotent per key)
hub schedules delete <name> --yes
```

Exactly one of `--prompt`, `--prompt-file`, `--prompt-id` (optional `--var K=V`).
Each fire enqueues into the same durable `session_deliveries` queue as webhooks
(queued when the session is busy/down, redelivered when back, idempotent per
schedule + occurrence time). Works for any interactive engine (claude, codex,
zcodex, opencode). If Hub is unreachable for several occurrences, exactly one
catch-up fire happens (no backfill storm); pause/resume does not backfill. Ticks
run on the 5-minute Hub cron, so the effective resolution is 5 minutes.
Audit rows hold ids/statuses only, never prompt bodies.

## `--prompt-id` everywhere

- `hub sessions send <id> --prompt-id oncall-triage --var k=v` renders in Hub and sends.
- `hub webhooks create ... --prompt-id alert-template --var team=dev` makes the
  library prompt the route template (payload placeholders available).
- `hub schedules create ... --prompt-id orchestrator-hourly`.

## UI

`/prompts.html`: prompt library, schedules (pause/resume/fire now) and recent
schedule deliveries with status.
