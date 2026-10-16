# Agent Hub

Generic agent-fleet hub on Cloudflare: authenticated messaging and dashboards, machine subscriptions and shared account quotas, placement of headless agents, and real interactive sessions. See [the generic hub guide](docs/GENERIC-HUB.md) for configuration, CLI commands, execution boundaries and migration. The intended production URL is `https://hub.dtcdev.click`; deployment and physical host acceptance are separate from source availability.

Serverless coordination hub for Alexey's multi-agent project: 30-minute headless root runs on a random available host, the daily writer job, machine registry, message bus, health and dashboard.

The spec is [docs/SPEC.md](docs/SPEC.md); it is the source of truth for scope and milestones.

See `AGENTS.md` for team and process rules.

Task routing supports capability matching, batches, client-owned asynchronous jobs and configurable model tiers. See [routing API, CLI and host setup](docs/ROUTING.md).

## Root scheduling flag

The hub can pause and resume root-run scheduling through a persisted flag. It is
stored in the Hub Durable Object's SQLite (`root_scheduling` table, single row),
so it survives restarts and is checked inside the same storage transaction that
creates offers and claims: there is no flag-check race window.

- **Default: enabled.** An uninitialized or upgraded deployment keeps the exact
  pre-flag scheduler behavior.
- **Disable (`enabled: false`) atomically prevents:**
  - the scheduler tick from creating a root-run job for a slot (no job, no offers),
  - any new offer of a root-run job (fallback after decline/timeout included),
  - new claims of root-run jobs, including jobs that are already offered,
  - root lease takeovers, and root-run START transitions (claimed -> running).
- **Frozen while disabled:** a root-run job that was already offered stays
  `offered` (no offer expiry, no re-offer to another host) and is hidden from
  `GET /api/jobs/offers`, `GET /api/host/offers` and `POST /api/host/poll`.
- **Not affected:** a root run that is already `running` keeps its lease; renew
  and finish continue to work. Disabling never kills a live root. Lease expiry
  and the no-first-action (claimed, not started) expiry continue.
- **Deadline:** a root-run job whose `deadline_at` has passed is never offered
  again. When its offer times out or is declined it is closed as usual
  (`no-host` if no candidate is left), otherwise as `expired` with
  `deadline passed before claim` instead of going to another host. While
  disabled, alarm processing closes such a job as `expired` too. Re-enabling
  after a long pause therefore leaves no stale offer for an old slot.
- **Idempotent:** repeated PUTs with the same value change nothing (including
  `updated_at`); disable followed by enable restores normal scheduling.

### API

Write authority is the existing admin session (DTC login, `HUB_ADMINS`
allowlist) only. `PUT` requires the session CSRF header; the body is strict.
Bearer tokens (host, agent, job/lease tokens) cannot change the flag.

Read authority is the admin session or an existing host bearer token, verified
the same way as `POST /api/host/poll` (`host` scope, kind `host`, not revoked,
not expired, issued to a registered machine). The host is identified by its
verified credential only. Unknown, revoked or expired tokens, and agent, job or
admin-scoped bearer tokens, get `401`.

`GET /api/root/scheduling` — read the state:

```
GET /api/root/scheduling
Cookie: hub_session=<session cookie>
  (or) Authorization: Bearer <host access token>

200 OK
{"enabled": true, "updated_at": "2026-10-14T18:00:00.000Z", "server_time": "2026-10-14T18:01:02.345Z"}
```

`updated_at` and `server_time` are UTC (ISO 8601, `Z`). `updated_at` reports
the last actual change (the table creation time if never changed); it is `null`
only if the row is missing (then the safe default applies).

`PUT /api/root/scheduling` — set the state:

```
PUT /api/root/scheduling
Cookie: hub_session=<session cookie>
X-CSRF-Token: <csrf token from GET /auth/me>
Content-Type: application/json

{"enabled": false}

200 OK
{"enabled": false, "updated_at": "2026-10-14T18:05:00.000Z", "server_time": "..."}
```

Failures:

| Status | Error | When |
| --- | --- | --- |
| 401 | `unauthorized` | `GET`: no admin session and no valid host bearer. `PUT`: no admin session (all bearer tokens) |
| 403 | `forbidden` | Session is not an admin (defensive; non-admin sessions are normally rejected as 401) |
| 403 | `CSRF token missing or invalid` | `PUT` without a valid CSRF header |
| 400 | `malformed_json` | Body missing, not valid JSON, or over 32 KiB |
| 400 | `invalid_body` | Body is not exactly `{"enabled": <boolean>}` (extra or unknown fields rejected) |
| 400 | `invalid_enabled` | `enabled` is not a boolean |
| 405 | Method Not Allowed | Anything but `GET`/`PUT` |

Related gating failures while disabled: `POST /api/jobs/:id/claim` on a root-run
job returns `409 {"error": "conflict"}`; `POST /api/jobs/:id/start` on a root-run
job returns `409 {"error": "root_scheduling_disabled"}`; root takeover returns
`409 {"error": "root_scheduling_disabled"}`. Each state change is recorded in the
`audit` table (`root_scheduling_enable` / `root_scheduling_disable`).

The examples above are tokenless: they show placeholder cookie, bearer and CSRF
values only, never a real credential.

## License

MIT

