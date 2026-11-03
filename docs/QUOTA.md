# Hub-Server Quota and Retired Host Snapshot API

## Primary collection: Hub server

Provider accounts are probed by the Hub backend on Hetzner through the maintained
`quse` adapters. `agent-hub-quota-server.timer` runs independently of the fleet
host agent, enrolled machine availability and PocketShell, every five minutes.
The Worker stores only sanitized observations, never provider credentials.

Install the `host/agent_hub_host` package in
`~/.local/lib/agent-hub-quota-server/host/agent_hub_host`, and install the service
and timer from `deploy/`. Configure `~/.config/agent-hub/quota-server.json` with
`base_url`, `admin_token_file` (the existing private Hub bootstrap token), `quse`
(the absolute maintained executable), and `accounts` (a list of `{account,
provider}` mappings). Account labels must match the fleet registry. The timer
uses server-local provider authentication; it does not obtain credentials from
machines or clients. Configure any Go key override in the service environment;
otherwise the maintained probe reads the server's OpenCode auth store.
Centrally provisioned account labels are also discovered automatically from
`managed-engines/state.json` (relative to the host config directory). The bounded
`hub engines quota` adapter provides isolated account observations. Optional
`managed_state_file` and `managed_quota_argv` select the installed managed catalog
and CLI location; adding an account requires no separate collector mapping. No paid
fallback, credit purchase or account creation is part of collection.

`POST /api/quota/server` requires an admin session and CSRF token. The bounded
32 KiB body is `{captured_at, accounts:[{account, provider, quota}]}`. Each quota
uses the same sanitized schema 1.1 route DTO documented below. The whole batch
is validated before storage. Duplicate accounts, mismatched providers, private
fields, future measurements and replayed/older snapshots are refused. Failed
probes explicitly replace previous success with unavailable evidence.

The latest row per account lives in `server_quota_snapshots`. The fleet quota
view uses only this Hub-server observation: missing, stale or failed server
readings are never filled from a machine subscription or host snapshot.
`GET /api/quota?view=fleet` returns account `pools`, `source`, `windows` and
`account_coverage`; the former per-host `hosts` and machine `coverage` fields
are omitted. Legacy `GET /api/quota` host-scoped reads return
`410 machine_quota_collection_disabled`; historical host rows are retained but
not exposed. Operators who
already have the private bootstrap credential can read with
`hub quota --admin-token-file ~/.config/agent-hub/admin-token`, without issuing a
new credential or changing any machine's login profile.
The machine-side `hub subscriptions` collection command is retired and refuses
to probe or report quota; use `hub quota` for the Hub-server projection.

## Host snapshots


Status: Founder-authorized specification ([Issue #12](https://github.com/alexeygrigorev/agent-hub/issues/12)).
Implementation scope: Hub server (`src/quota.ts`, `src/hub.ts`) and host collector (`host/agent_hub_host/`). Downstream consumer adapters (`agent-quota-launcher` and `cloudflare-agent-git/scripts/quota-gate.py`) remain owned by their respective delivery coordinators.

## 1. Purpose and architectural boundary

`QUOTA-HUB-SPEC-20261014` defines an expressly authorized exception to the general rule against storing quota figures in Agent Hub:

**Current quota authority (intake 58ebeb9f):** fleet quota comes only from the
Hub server. The maintained HostAgent no longer schedules the former 600-second
host snapshot collector. The legacy DTO validator and historical rows remain
for a safe transition; the retired host API neither reads nor writes those rows.

- **60 s heartbeats remain boolean-only**: Regular host heartbeats (`POST /api/host/poll` and WebSocket `heartbeat` frames, [docs/SPEC.md §7.2](SPEC.md#72-heartbeat-and-sanitized-quota-snapshot-quota-hub-spec-20261014)) continue to report only the boolean `gate: { launch_allowed, reason, at }` for scheduling and never run `quse` on every beat.
- **Historical 600 s snapshot contract**: The former host collector posted one bounded `quse --json` snapshot every **600 seconds (10 minutes)** to `POST /api/quota`. Maintained HostAgent no longer schedules this collection.
- **Historical single-row per-host retention**: Existing rows in `host_quota_snapshots` (`host_id TEXT PRIMARY KEY`) are retained for audit and migration only. No public quota read exposes them, and they never contribute to fleet quota or routing.
- **No host read or collection route**: `GET` and `POST /api/quota` return `410 machine_quota_collection_disabled` to valid legacy callers. Neither operation reads or writes machine snapshots.

## 2. Legacy host snapshot DTO (`schema_version: "1.0"`)

This schema is retained to validate legacy submissions and interpret historical
rows. A valid host submission is rejected with `410 machine_quota_collection_disabled`;
it is never written to `host_quota_snapshots`.

### 2.1 `POST /api/quota` request body

```json
{
  "schema_version": "1.0",
  "captured_at": "2026-10-14T05:40:00.000Z",
  "host_id": "mach_01hxyz...",
  "status": "ok",
  "error": null,
  "routes": {
    "codex": {
      "status": "ok",
      "error": null,
      "observed_at": "2026-10-14T05:38:15.000Z",
      "details": {
        "limit_reached": false
      },
      "windows": {
        "5h": {
          "percent_remaining": null,
          "reset_at": null,
          "present": false,
          "rolling": true
        },
        "7d": {
          "percent_remaining": 78.5,
          "reset_at": "2026-10-20T00:00:00.000Z",
          "present": true,
          "rolling": false
        }
      }
    },
    "grok": {
      "status": "ok",
      "error": null,
      "observed_at": "2026-10-14T05:40:00.000Z",
      "details": {
        "limit_reached": false,
        "has_grok_code_access": true
      },
      "windows": {
        "5h": {
          "percent_remaining": 92,
          "reset_at": "2026-10-14T10:40:00.000Z",
          "present": true,
          "rolling": true
        }
      }
    }
  }
}
```

### 2.2 Retired `GET /api/quota` response

The endpoint authenticates legacy callers, then returns HTTP `410 Gone` with
`{"error":"machine_quota_collection_disabled","source":"hub_server"}`.
It never returns the former fresh, stale or missing host snapshot bodies.

### 2.3 Field validation rules

| Level | Field | Constraint |
| --- | --- | --- |
| Top-level | `schema_version` | Exact string `"1.0"`. |
| Top-level | `captured_at` | Required UTC-aware ISO-8601 timestamp (`Z` or `+00:00`), normalized to `.toISOString()`. Rejects naive timestamps, non-UTC offsets, invalid calendar dates, future timestamps (`> server_now`), and replay/out-of-order timestamps (`<= existing.captured_at`). |
| Top-level | `host_id` | Required non-empty machine ID matching the authenticated host credential's `machine_id`. |
| Top-level | `status`, `error` | `status: "ok" \| "error" \| "unknown"`, `error: null \| "unavailable"`. When `status === "ok"`, `error` must be `null`. |
| `routes` | `<provider>` | Strict allowlist of `quse` providers: `codex`, `claude`, `gemini`, `grok`, `zai`, `go`, `copilot`. Unknown provider keys are rejected (`400`). |
| `routes.<provider>` | `status`, `error` | `status: "ok" \| "error" \| "unknown"`, `error: null \| "unavailable"`. Any raw upstream error text is forbidden. |
| `routes.<provider>` | `observed_at` | `null` or UTC-aware ISO-8601 timestamp (`<= captured_at` and `<= server_now`). Preserves older provider-native observation timestamps without overwriting them with `captured_at`. |
| `routes.<provider>.details` | `limit_reached` | Required `boolean \| null`. |
| `routes.<provider>.details` | `has_grok_code_access` | `boolean \| null`, allowed **only** when `<provider> === "grok"`. Rejected (`400`) on any other provider. |
| `routes.<provider>.details` | `stale` | Optional `boolean \| null`. When `details.stale === true`, the route is forced fail-closed to `status: "error", error: "unavailable"`. |
| `routes.<provider>.windows` | `<window_name>` | Data-driven window names validated against `^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$`, bounded to at most **32 windows per provider**. Examples include `"5h"`, `"7d"`, `"monthly"`, `"daily"`, etc. Window names are preserved verbatim and never remapped or inferred to `"5h"`. |
| `windows.<name>` | `percent_remaining` | Finite `number` in `[0, 100]` or `null`. Rejects `NaN`, `Infinity`, `< 0`, `> 100`, booleans, and strings. `null` remains `null` and is never coerced to `0`. |
| `windows.<name>` | `reset_at` | `null` or UTC-aware ISO-8601 timestamp (`Z` or `+00:00`), normalized to `.toISOString()`. Rejects empty strings, naive timestamps, and non-UTC offsets. |
| `windows.<name>` | `present`, `rolling` | Required `boolean`. When `present === false`, `percent_remaining` and `reset_at` must be `null`. |
| `windows.<name>` | `error` | Optional `null \| "unavailable"`. Any explicit window `error: "unavailable"` (even on an optional window with `percent_remaining: null` and `reset_at: null`) marks both the window (`error: "unavailable"`) and its parent provider route (`status: "error", error: "unavailable"`) fail-closed, never `"N/A"`. Empty optional `percent_remaining`/`reset_at` without error remains `present: false` / `null` (no fake `0`). |

## 3. Missing Codex `5h` window handling

- When OpenAI Codex does not return a 5-hour window, `"5h"` is represented honestly as absent (`present: false, percent_remaining: null, reset_at: null`) or omitted if not reported by upstream.
- Weekly (`"7d"`) quota data is **never** copied, aliased, or substituted into `"5h"`.
- When a canonical `"5h"` window is genuinely observed in `quse --json`, it is preserved with `present: true` and its observed `percent_remaining` and `reset_at`.

## 4. Authentication and machine isolation

1. **`POST /api/quota` (retired)**:
   - Requires `Authorization: Bearer hubt_...` backed by a non-revoked, non-expired `credentials` row with `kind === "host"`, scope `"host"`, and a `machine_id` registered in `machines`.
   - Host identity and DTO validation remain enforced for legacy clients. A valid DTO receives `410 Gone` with `{ "error": "machine_quota_collection_disabled", "source": "hub_server" }`; it is not stored. `body.host_id` must equal `cred.machine_id` (`403 Forbidden` on mismatch).
   - Anonymous callers (`401`), expired/revoked tokens (`401`), `job` tokens (`403`), `agent` tokens (`403`), `human` tokens (`403`), and admin session cookies (`403`) are denied before any SQLite write.
   - Host-spoofing headers (`X-Host-Id`, `X-Machine-Id`, `X-Forwarded-Host`) and mismatched `?host_id` query parameters are rejected (`403`).
2. **`GET /api/quota` (retired host read)**:
   - A valid host bearer or verified admin session receives `410 machine_quota_collection_disabled`; no host snapshot content is returned.
   - Invalid, expired or revoked credentials remain denied by the existing authentication checks before the retirement response.
3. **Response caching**:
   - Every `/api/quota` response sets `Cache-Control: private, no-store`.
4. **Host machine binding**:
   - `POST /api/machines/register` returns `{ "status": "registered", "machine_id": "<cred.machine_id>" }`. The `machine_id` is taken only from the authenticated host credential, never from the request body, so a host token can never learn or bind another host's id.
   - The host agent registers on every start and persists that `machine_id` with its origin and conflicting-id checks, so hosts paired before this field existed learn their id on the next start without re-pairing. Until `machine_id` is bound in the host config the collector holds and runs no `quse`.

## 5. Privacy and Free-plan bounds

- **Strict allowlist and unknown-key rejection**: No account identifiers, emails, billing records, dollar/credit balances, subscription names, raw error strings, token counts, usage logs, or transcripts may be sent or stored. Any unknown key at top-level, route-level, details-level, or window-level causes the entire payload to be rejected (`400 Bad Request`) prior to any SQLite transaction, and rejected values or credentials are never echoed in error responses or logs.
- **32 KiB body cap**: Request bodies are bounded to `32,768` bytes both via `Content-Length` and incremental stream chunk accounting (`413 Payload Too Large`).
- **Historical storage only**: Existing `host_quota_snapshots` rows are retained for audit and migration. The retired host API performs no insert, update, or delete on this table, and public views do not read it.

## 6. Retired collection and historical staleness boundary

- **No host collection**: Maintained HostAgent does not schedule `quse` quota collection. The Hub rejects valid host submissions with HTTP `410` and does not create or update machine snapshot rows.
- **No controller collector**: `session_host` maintenance runs fleet dispatch only; it no longer constructs, schedules, or ticks a per-machine quota collector.
- **Standalone host runner retired**: `python -m agent_hub_host.quota_snapshot` exits with a retirement message before probing or submitting quota data.
- **Legacy collector DTO validation**: The retired DTO validator still rejects malformed, secret-bearing, future, and oversized submissions before returning the retirement response. It exists for a safe transition and historical data handling, not as an ingestion path.
- **Derived quota history**: Resource history, TBM pools and banked-reset alerts use Hub-server account observations. Legacy machine subscription and snapshot rows are ignored.

## 7. Client caching and fallback contract

Downstream consumers (`agent-quota-launcher` and `cloudflare-agent-git/scripts/quota-gate.py`):

1. Read `GET /api/quota` using the host's existing machine-bound credential. The retired endpoint returns `410`, so it supplies no quota reading.
2. Downstream launchers may still perform their separately maintained local admission checks. Those checks do not post per-machine quota to Hub, and the Hub fleet view, routing and reset alerts never use their results.
3. Apply strict per-route gates to the Hub answer or to the single local result. Each gate that is not met fails closed:
   - **Missing route**: the required provider route must exist. A `200` with `routes: {}` is a valid snapshot that admits nothing.
   - **Per-route status/error**: the route must have `status: "ok"` and `error: null`. A top-level `status: "ok"` does not imply that each route is ok; a `200` with one route in `error` is normal.
   - **Unknown `limit_reached`**: `details.limit_reached` must be exactly `false`. Both `true` and `null` (unknown) fail closed.
   - **No present numeric windows**: the route must have at least one window with `present: true` and a numeric `percent_remaining`. A route whose windows are all absent never passes a threshold check vacuously.
   - **Provider/window thresholds**: thresholds apply per window and only to present, known values. An absent or errored window (`present: false`, `percent_remaining: null`, or `error: "unavailable"`) is unknown: never `0`, and never aliased to or satisfied by another window. In particular, an unknown Codex `5h` stays unknown and is never replaced by `7d`. Codex protected admission still needs a known `percent_remaining > 15` on every window the requirement needs, so an unknown required window fails closed. Grok fails closed at `<= 5%` remaining.
   - No failed, stale or unknown route is ever admitted, whether it came from the Hub or from the local fallback.
4. Thresholds, the `30 s` cache and the one-`quse` fallback are consumer-side only. The Hub does not enforce them. The consumer code itself (`agent-quota-launcher`, `cloudflare-agent-git/scripts/quota-gate.py`) belongs to those repos' owners and is not part of this repository.

## 8. Authority limits and bootstrap hold

- **No Root or task authority**: Quota snapshots are read-only admission telemetry and never grant or renew `root` leases, job claims, or bus actor custody.
- **Offline source and activation hold**: All repository tests use offline fixtures with fake OIDC/SQLite/Miniflare state (`test/quota.test.ts`). Live Cloudflare deployment, host credential bootstrap, and systemd service activation remain held under existing owner and founder approval gates. Rejected issue #113 artifacts remain uninstalled.
