# Fleet OpenCode Muse-brief route (platform-muse-brief slice)

Exact source/config contract for head integration. No production launches by worker.

## Source
- `host/agent_hub_host/fleet.py` (narrow): headless dispatch selects backend by `launcher_provider` — `opencode` uses maintained launcher `run --backend aplexer` (real native UUID + first-action evidence for M12 bind/control); all other providers preserve `run --backend task-units --as-controller` + semantics. Receipt handling unchanged (session_id preferred, then unit_name/worker_id; running requires state running + first_action_verified true; exit_code int gives exited/failed with confirmed_exit).
- `host/agent_hub_host/fleet_launch.py` (NEW, no native effects): `opencode_subscription()` (service go, engine opencode, pinned model, maintained admission/launcher config), `opencode_offer()` (arbitrary brief verbatim), `launcher_payload()` (exact submit payload), `launcher_run_argv()` (exact aplexer-backend run argv, never task-units for opencode), `validate_native_receipt()` (requires session_id + running + first_action_verified true + engine opencode; unit-only receipts rejected for binding).

## Config
- Subscription: service `go`, engine `opencode`, account_label as configured, admission_argv = maintained fresh gate, launcher = {argv:[python3 -m launcher], root:<launcher checkout>, config_dir:<launcher config>}, launcher_provider `opencode`, launcher_model `opencode-go/muse-spark-1.3-contributor`.
- Offer: mode headless, engine opencode, workspace label configured, prompt = arbitrary brief (verbatim, separate argv downstream, no shell interpolation), optional task_id for report/file outcome via `.local/hub-results/<offer>.json`.
- Launcher side (see launcher docs/opencode-muse-brief.md): quse go windows + `opencode models` containing exact model + runtime binary + account/profile checks; `plan` must list eligible opencode/muse-spark with account/profile match.

## Binding
- Launcher aplexer backend prints JSON receipt with session_id (native UUID), engine opencode, first_action_verified true on success. Fleet dispatch returns `{state: running, native_id: <UUID>, launcher_task_id: hub-<offer>}` and POSTs `/api/fleet/launches/<offer>/result`. Automatic Hub session binding via existing `once()` reconciliation (tag `hub-<fleet_launch_id>` + engine match) applies to interactive sessions; headless opencode via launcher aplexer backend uses `task-hub-<offer>` tags (launcher `run_tag_for`) — head to confirm canonical binding seam for launcher-backend sessions (worker did NOT change reconciliation or peer scopes; ask head if exact seam needed).
- Task-units `unit_name`-only receipts never validate as native opencode bindings (see test).

## Tests
- `host/tests/test_fleet_opencode.py` (4 tests): opencode headless uses aplexer backend (no task-units/--as-controller), others preserve task-units, unit-only receipt rejected / UUID receipt accepted, helper contract exact.
- Existing `host/tests/test_fleet.py` headless/launcher tests green (interactive, unsupported gate, headless plan). Note: `test_finite_cycle_registers_capabilities...` fails pre-existing on main (expects 5 fleet calls, actual includes `/api/sessions/host-bindings` reconciliation added without test update) — out of scope, untouched, flagged for head.
