# Quota Dashboard

The fleet quota view reads provider usage only from Hub-server observations in
`server_quota_snapshots`. It does not merge host subscriptions or host quota
snapshots, and it does not expose per-machine quota rows. Account definitions
and routes still supply the account, harness and profile labels shown beside
the Hub reading. The legacy host snapshot API is retired for reads and writes.

## Schema versions

- `1.0`: exact legacy contract (`percent_remaining`, `reset_at`, `present`,
  `rolling`, `error`, `limit_reached`, `stale`, `has_grok_code_access` on grok).
  Payloads containing any newer key are rejected (`400`) — explicit version
  negotiation, so old readers/writers keep working byte-for-byte.
- `1.1` (additive): optional envelope `collector_version` (bounded ASCII label,
  e.g. `quota_snapshot/1.1`) and optional per-window / per-details native
  fields: `credits_remaining` (finite `0..1e12`), `spend_used`/`spend_limit`
  (finite `0..1e15`), `unit` (`percent|credits|usd_cents|requests|tokens`),
  `profile` (bounded label, e.g. a Muse Spark model id). Absent natives stay
  `null`/`unknown`; percentages are never converted to dollars or tokens.
  Unknown or secret keys (tokens, cookies, emails, raw output, private config)
  are rejected (`400`) in both versions.
- An absent window (`present: false`) carries `null` measurements and no error.
  A window with `error: "unavailable"` fails its route closed even when sibling
  windows look healthy.

## API

- `POST /api/quota`: retired. Authenticated, valid legacy submissions return
  `410 machine_quota_collection_disabled` and do not write machine snapshots.
- Fleet quota uses only Hub-server account probes and `server_quota_snapshots`;
  machine snapshots and subscription reports do not supply fleet quota.
- `GET /api/quota`: retired host-scoped read. A valid host bearer or verified
  admin session receives `410 machine_quota_collection_disabled`; host snapshot
  rows are not exposed.
- `GET /api/quota?view=fleet[&host_id=&provider=&engine=&account=]`: fleet-wide
  projection. Grants reuse the canonical Hub authority
  (`authenticateBusRequest`): human admins (session or admin bearer),
  `agent` bearers with `fleet:launch` scope, `routing-client` bearers with
  `routing:submit`, and host bearers with their fleet read grant; revoked or
  expired callers are denied by canonical auth.
- The fleet body (`schema_version "1.1"`, `Cache-Control: private, no-store`)
  carries `pools[]` (one row per canonical account, sourced only from the Hub
  server) and `account_coverage`. It omits the former per-host `hosts` and
  machine `coverage` fields. Each pool identifies its source as `server`; an
  absent reading remains unknown and a stale server reading remains stale.
- Pool states: `fresh`, `stale`, `exhausted` (`limit_reached`), `unavailable`
  (route/window error), `unknown` (no fresh evidence). A passed `reset_at`
  alone never marks capacity recovered.

## Admission (`src/quota_admission.ts`)

`evaluateQuotaAdmission({provider, quota, subscriptionObservedAt, nowMs,
reserves})` is the single gate used by `accountStatus`, `plan`, `dispatch`
(model shortlist filter), `modelCandidates` (approved one-line hook; policy,
ranking and preferences unchanged) and `claim` (re-checked atomically before
effect).
Reserves come from `config/model-routing.json rules.reserve_percent`
(Codex 15, Grok 5) applied to **every** available applicable window.
Fail-closed on: stale/future/malformed observation times, `status !== "ok"`,
`limit_reached !== false`, missing Grok code entitlement, any
`error: "unavailable"` window, malformed numbers/dates, reset passed without a
post-reset observation, zero available windows, and a known active provider
backoff hold (`details.cooldown_until` in the future, mirroring the maintained
launcher's 429 Retry-After `cooldown_until` authority). Benign absence of an
optional unsupported window (`present: false`, nulls, no error) is ignored.
Model preference (Muse Spark 1.3, Z.ai `glm-5.3-flash`) applies only among
eligible routes; exhaustion gates are never overridden by any manual flag.

Cooldown holds (`details.cooldown_until`, schema 1.1, bounded UTC ISO) mirror
the maintained launcher/provider-capacity 429 authority without duplicating it:
no second table or controller exists. A newer report that omits the field never
erases an unexpired stored hold (retained in the existing snapshot and
subscription rows); updates extend it; expiry plus fresh percent recovers.
Coverage limitation, stated explicitly: installed `quse` emits no cooldown
field today, so live holds arrive only when a host collector observes a real
provider backoff; the contract, retention and gating are proven by fixtures,
and no deadline is ever invented.

## CLI and UI

- `hub quota [--host ID] [--provider P] [--engine E] [--account L] [--json]`
  (`host/agent_hub_host/quota.py`): one authenticated `GET`, text table by
  default, `--json` for machines. Extends the existing parser; status, intake,
  handoff and session commands are untouched.
- Dashboard `public/fleet.html` quota card + `public/fleet.js renderQuota()`:
  existing dakit classes/components only, `textContent`/DOM rendering,
  pool states, native units, resets, account/profile attribution and coverage
  counted across Hub-managed accounts. Exhausted accounts display `0%` and the
  reset time when the Hub reading provides one.

## Verification

Collector/CLI Python suites plus `test/quota-m13.test.ts` (HTTP/SQLite
integration: native roundtrip, window error vs benign absence, shared-pool
dedupe, revocation, privacy, reset/malformed negatives, dispatch → exhausted →
claim refusal) and `test/quota-m13.browser.mjs` (same UI/API contract,
390/1440 light/dark). Live `quse` today reports percent-only windows, so live
native fields stay `null`; non-null roundtrip is proven by schema-valid
fixtures, never synthetic live zeros.
