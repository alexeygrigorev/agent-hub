# M9 metrics producer (host-side freshness)

Persistent bounded Dashboard -> Hub producer in the existing `session_host`
loop. No extra timer/service/dispatcher. Missing config means disabled.

## Ownership

- NEW: `host/agent_hub_host/metrics_producer.py`, `host/tests/test_metrics_producer.py`
- NARROW HOOK: `host/agent_hub_host/session_host.py` (`metrics_step_once`,
  `maybe_start_metrics_thread`, one `MetricsProducer` + one thread in `run()`)
- OPTIONAL EXPORTER: `scripts/metrics-dashboard-export.py`
- Peer scopes untouched: `session_executor.py`, `session_receiver.py`,
  `native_sessions.py`, `fleet.py`, TS metrics auth/UI.

## Runtime

- Config: `<journal>/metrics-producer.json` (operator-controlled allowlist).
  Example (no secrets):
  ```json
  {
    "machine_id": "mach_…",
    "min_interval_s": 300,
    "dashboard_src": "/home/alexey/git/agent-dashboard/src",
    "output_dir": "/home/alexey/private/metrics",
    "exporter_timeout_s": 30,
    "post_timeout_s": 10
  }
  ```
  Optional `exporter_argv` (1–16 tokens) overrides the built-in trusted
  exporter invocation. Unknown keys reject. `min_interval_s` clamps 60–3600
  (default 300). Timeouts bounded 5–120s (exporter) / 5–30s (post).

- Confinement: `dashboard_src` and `output_dir` must be absolute under
  `/home/` (realpath), output never inside dashboard source, never under
  `/global/data` (FULL). Output file is `<output_dir>/metrics-snapshot.json`
  (0600 via `private_fs.atomic_write`). Exporter argv absolute tokens must
  stay under `/home/` (interpreter + flags excepted).

- Loop: existing `session_host.run()` starts/skips ONE daemon
  `metrics-produce` thread (`maybe_start_metrics_thread`); blocked exporter
  or HTTP never delays commands/quota. Producer owns cadence; `step()`
  returns `rate_limited` silently, `disabled` when config missing, `posted`
  only on validated fresh POST.

- Exporter (`scripts/metrics-dashboard-export.py`): read-only import of
  `dashboard.server._load_hourly_payload/_load_usage_payload/_load_features_payload`
  via explicit `--dashboard-src`, actual UTC now (or strict `--as-of`),
  then `metrics.project_snapshot` (sanitized strict v1 DTO, quota/cost
  dropped, real coverage/unknown, `as_of`/provenance preserved). Writes only
  the owned private snapshot. Never mutates dashboard repo/services/config.

- HTTP: `metrics.post_metrics_snapshot` (existing `api.safe_http_json`,
  dedicated host credential from host config, never admin token). Freshness:
  `as_of` must parse (strict UTC) and be within [-300s future, 7200s stale];
  otherwise `stale_snapshot`, no POST, no fake freshness. Malformed/missing
  inputs keep `unknown`/`partial`, never zero.

- Logs: minimal failure class only (`disabled`, `invalid_config`,
  `missing_machine_id`, `exporter_failed`, `exporter_timeout`,
  `invalid_snapshot`, `stale_snapshot`, `submit_failed`, `rate_limited`).
  No config/token/stderr/argv/report data.

## Limits

- Config 16 KiB; exporter stdout/stderr 64 KiB each; snapshot 32 KiB;
  output 256 KiB; subprocess timeout bounded; no `shell=True`.
- Host loop: `<=1500 MiB`, `TMPDIR=<repo>/.local/tmp`, scratch `<=32 MiB`.
- Dashboard repo read-only; Hetzner-only; Win35 offline expected.

## Tests

`PYTHONPATH=host python3 -m unittest host.tests.test_metrics_producer -v`
covers: disabled missing config, invalid config closed, cadence floor 60s,
singleflight skip/restart, valid sanitized submit, stale/malformed/missing
no-fake-freshness, timeout class isolation, output-dir confinement, blocked
producer never delays command loop. Existing `test_metrics*` +
`test_session_host*` suites stay green.
