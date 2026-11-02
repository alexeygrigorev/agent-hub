# Worker-scoped engine verification view

Intake f28929eb: a worker-scoped session (e.g. 598e9345 on Tabo) gets
`403 worker_scope_forbidden` from `hub engines list` and `hub fleet show`.
This document records the decision and the remedy.

## Decision

The 403s are **by design**. Worker scope (`worker:own-intake`) is an explicit
narrow allowlist (`workerRequestAllowed` in `src/worker-access.ts`); it grants
intake access to its own launch plus read-only quota (`/api/quota?view=fleet`)
and sanitized fleet resources. The operator surfaces stay outside it:

- `GET /api/engines` (admin only) returns engine bundles, managed logins,
  installations, credential status, credential rejections and account
  migrations — exactly the material worker credentials must not read.
- `GET /api/fleet` (admin only) returns full fleet state: every machine,
  session, launch and placement record.

So the bug was never the 403; it was that a worker session had **no way at
all** to verify the engines it was launched with. Workers legitimately need
to answer "which engine/account am I supposed to be using on this host, and
is it healthy?"

## Remedy: `GET /api/engines?view=worker`

A read-only, sanitized view available to worker-scoped sessions. Rules:

- Worker scope required: `caller.kind === 'agent'` with scope
  `worker:own-intake`. Admin and host credentials calling the same URL get
  `403 forbidden` — the view is scoped to the worker capability, not a
  general downgrade of `/api/engines`.
- Exactly one `view` parameter with value `worker`; anything else on the same
  path falls through to the operator surface and its normal enforcement
  (workers still get `worker_scope_forbidden` there).
- `GET` only.

Response body:

```json
{
  "schema_version": "1.0",
  "observed_at": "2026-10-31T00:00:00.000Z",
  "engines": [
    {
      "engine": "opencode",
      "account": "shared-go",
      "host": "tabo",
      "health": "unverified",
      "quota_state": "ok"
    }
  ]
}
```

One row per fleet route (`fleet_routes`), sorted by engine, account, host.
Every field is a string — **no nulls for known fields** (the original intake
complaint). `schema_version` is `"1.0"`.

Field semantics:

- `engine` — harness engine on the route (`codex`, `opencode`, `go`, `zai`,
  `claude`, …).
- `account` — canonical account label (legacy labels such as `urgent-muse`
  canonicalize to `shared-go`), matching `/api/quota?view=fleet` pools.
- `host` — machine id the route serves.
- `health` — mirrors placement admission over `fleet_engine_health`:
  - `unavailable` — an `unavailable` row for the exact (host, engine) pair
    or engine-wide (`machine_id = ''`); placement would refuse this route.
  - `ok` — an `ok` row exists (pair or engine-wide) and nothing unavailable.
  - `unverified` — no health evidence recorded.
- `quota_state` — from the fleet-view quota pool for the account:
  - `ok` — fresh observation with known windows.
  - `exhausted` — fresh observation reporting `limit_reached`.
  - `unavailable` — fresh observation whose windows all errored.
  - `stale` — only outdated observations.
  - `unknown` — no observation evidence.
  - `disabled` — account exists in `fleet_accounts` but is disabled.
  - `unmapped` — route to an account with no registry row and no quota pool.

What is intentionally absent: engine bundles, versions, install paths,
managed logins, credential status, rejections, migrations, profiles,
remaining percentages, and any secret material.

## CLI

```
hub engines list --view worker
```

Worker sessions authenticate through their launch credential
(`worker_auth.py`); operators running the same command without worker scope
get `403 forbidden` and should use plain `hub engines list` instead.

## Operator restrictions preserved

Nothing about the existing surfaces changed: `GET /api/engines` and
`GET /api/fleet` remain admin-only, the worker allowlist still denies every
other path/method on `/api/engines*` with `worker_scope_forbidden`, and the
fleet view (`/api/quota?view=fleet`) keeps its existing one-view-param rule.
