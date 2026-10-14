# Host Quota Snapshot API (`QUOTA-HUB-SPEC-20261014`)

Status: Founder-authorized specification ([Issue #12](https://github.com/alexeygrigorev/agent-hub/issues/12)).
Implementation scope: Hub server (`src/quota.ts`, `src/hub.ts`) and host collector (`host/agent_hub_host/`). Downstream consumer adapters (`agent-quota-launcher` and `cloudflare-agent-git/scripts/quota-gate.py`) remain owned by their respective delivery coordinators.

## 1. Purpose and architectural boundary

`QUOTA-HUB-SPEC-20261014` defines an expressly authorized exception to the general rule against storing quota figures in Agent Hub:

- **60 s heartbeats remain boolean-only**: Regular host heartbeats (`POST /api/host/poll` and WebSocket `heartbeat` frames, [docs/SPEC.md §7.2](SPEC.md#72-heartbeat-and-sanitized-quota-snapshot-quota-hub-spec-20261014)) continue to report only the boolean `gate: { launch_allowed, reason, at }` for scheduling and never run `quse` on every beat.
- **600 s sanitized quota snapshots**: Each registered host collects one bounded `quse --json` snapshot every **600 seconds (10 minutes)** and posts a strictly sanitized Data Transfer Object (DTO) to `POST /api/quota`.
- **Single-row per-host retention**: The Hub Durable Object persists only the single latest validated snapshot per verified host in `host_quota_snapshots` (`host_id TEXT PRIMARY KEY`), keeping storage and row-write counts well within Cloudflare Workers Free plan limits.
- **`GET /api/quota` never triggers `quse`**: Reading `/api/quota` is a pure SQLite read inside the Hub Durable Object and never spawns a host process or triggers an on-demand `quse` probe.

## 2. Exact DTO contract (`schema_version: "1.0"`)

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

### 2.2 `GET /api/quota` response body

`GET /api/quota` returns the sanitized contract augmented with server-side `received_at` (canonical UTC ISO-8601) and `stale` (`boolean`), with header `Cache-Control: private, no-store`:

- **Fresh (`age < 1200 s`, HTTP `200 OK`)**: `stale: false`, `status: "ok"`, `error: null`.
- **Stale (`age >= 1200 s`, HTTP `503 Service Unavailable`)**: `stale: true`, `status: "stale"`, `error: "unavailable"`, and every provider route in `routes` is marked `error: "unavailable"` (`status: "error"` or `"unknown"`) while preserving `captured_at`, `received_at`, `observed_at`, `details`, and `windows` for diagnostics.
- **Missing (no snapshot stored for the host, HTTP `404 Not Found`)**:
  ```json
  {
    "schema_version": "1.0",
    "captured_at": null,
    "received_at": null,
    "host_id": "mach_01hxyz...",
    "stale": true,
    "status": "unknown",
    "error": "unavailable",
    "routes": {}
  }
  ```

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

1. **`POST /api/quota` (Host write only)**:
   - Requires `Authorization: Bearer hubt_...` backed by a non-revoked, non-expired `credentials` row with `kind === "host"`, scope `"host"`, and a `machine_id` registered in `machines`.
   - `body.host_id` must equal `cred.machine_id` (`403 Forbidden` on mismatch).
   - Anonymous callers (`401`), expired/revoked tokens (`401`), `job` tokens (`403`), `agent` tokens (`403`), `human` tokens (`403`), and admin session cookies (`403`) are denied before any SQLite write.
   - Host-spoofing headers (`X-Host-Id`, `X-Machine-Id`, `X-Forwarded-Host`) and mismatched `?host_id` query parameters are rejected (`403`).
2. **`GET /api/quota` (Own-host read or scoped admin session read)**:
   - **Host bearer token**: Reads only the caller's own `cred.machine_id` snapshot. No `?host_id` query parameter is needed; passing another machine's `host_id` returns `403 Forbidden`.
   - **Verified admin session (`hub_session` cookie)**: Must pass Origin, `Sec-Fetch-Site`, and optional `X-CSRF-Token` integrity checks, and **must** provide an explicit `?host_id=<machine_id>` query parameter (`400 Bad Request` if omitted). There is no global anonymous or unscoped multi-host feed.
3. **Response caching**:
   - Every `/api/quota` response sets `Cache-Control: private, no-store`.
4. **Host machine binding**:
   - `POST /api/machines/register` returns `{ "status": "registered", "machine_id": "<cred.machine_id>" }`. The `machine_id` is taken only from the authenticated host credential, never from the request body, so a host token can never learn or bind another host's id.
   - The host agent registers on every start and persists that `machine_id` with its origin and conflicting-id checks, so hosts paired before this field existed learn their id on the next start without re-pairing. Until `machine_id` is bound in the host config the collector holds and runs no `quse`.

## 5. Privacy and Free-plan bounds

- **Strict allowlist and unknown-key rejection**: No account identifiers, emails, billing records, dollar/credit balances, subscription names, raw error strings, token counts, usage logs, or transcripts may be sent or stored. Any unknown key at top-level, route-level, details-level, or window-level causes the entire payload to be rejected (`400 Bad Request`) prior to any SQLite transaction, and rejected values or credentials are never echoed in error responses or logs.
- **32 KiB body cap**: Request bodies are bounded to `32,768` bytes both via `Content-Length` and incremental stream chunk accounting (`413 Payload Too Large`).
- **Transactional replacement**: Validation completes before opening the synchronous SQLite write transaction. Only one row per `host_id` is stored.

## 6. Collection cadence (600 s) and staleness boundary (1200 s)

- **Host collection every 600 s**: The host agent schedules at most one bounded `[quse, "--json"]` subprocess (`shell=False`, timeout `<= 30 s`, bounded stdout) every `600 s` using a monotonic clock. Failed or unconfigured host states hold without spinning or leaking tokens.
- **Collector window consistency**: The host collector emits every absent or errored window as `percent_remaining: null, reset_at: null, present: false`, matching the Hub rule in §2.3. An errored window also carries `error: "unavailable"` and marks its own provider route `status: "error", error: "unavailable"`. An upstream explicit `present: false` with a numeric `percent_remaining` is contradictory and fails closed the same way. One bad window therefore marks only its own route unavailable; it no longer causes a `400` that drops every provider's data for that cycle.
- **Replay and out-of-order protection**: A post with `captured_at <= existing.captured_at` is rejected with `409 Conflict` so a cached or replayed post cannot reset `received_at` or extend snapshot freshness.
- **Conservative 1200 s (20-minute) staleness boundary**: Freshness on `GET /api/quota` is evaluated exclusively against the server's UTC clock (`Date.now()`), ignoring any caller-supplied clock headers or query parameters. When `max(server_now - captured_at, server_now - received_at) >= 1200 s`, the snapshot is marked `stale: true, status: "stale", error: "unavailable"` (`HTTP 503`).

## 7. Client caching and fallback contract

Downstream consumers (`agent-quota-launcher` and `cloudflare-agent-git/scripts/quota-gate.py`):

1. Read `GET /api/quota` using the host's existing machine-bound credential. Only a `200` with `stale: false`, `status: "ok"` and `error: null` is a usable Hub answer; cache it locally for a `30 s` TTL.
2. If the Hub query is inaccessible (network error, timeout) **or** returns anything else (stale `503`, missing `404`, any other non-`200`, `stale: true`, top-level `status` other than `"ok"`, a non-null `error`, or an unparseable body), run exactly **one** fresh bounded local `quse --json` (`shell=False`, timeout `<= 30 s`, stdout `<= 64 KiB`), sanitize it with the same rules as the host collector, and apply the gates below to that result. There is no second attempt and no retry loop. If that one run fails, times out, overflows or does not parse, fail closed. The body of a `503` or `404` is never used for admission.
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
