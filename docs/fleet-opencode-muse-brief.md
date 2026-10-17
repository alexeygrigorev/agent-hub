# Fleet OpenCode Muse-brief route (platform-muse-brief slice)

Exact source/config contract for head integration. No production launches by worker.

## Source
- `host/agent_hub_host/fleet.py` (narrow): headless dispatch selects backend by `launcher_provider` — `opencode` uses maintained launcher `run --backend aplexer` (real native UUID + first-action evidence for M12 bind/control); all other providers preserve `run --backend task-units --as-controller` + semantics. Receipt handling unchanged (session_id preferred, then unit_name/worker_id; running requires state running + first_action_verified true; exit_code int gives exited/failed with confirmed_exit).
- `host/agent_hub_host/fleet_launch.py` (NEW, no native effects): `opencode_subscription()` (service go, engine opencode, pinned model, maintained admission/launcher config), `opencode_offer()` (arbitrary brief verbatim), `launcher_payload()` (exact submit payload), `launcher_run_argv()` (exact aplexer-backend run argv, never task-units for opencode), `validate_native_receipt()` (requires session_id + running + first_action_verified true + engine opencode; unit-only receipts rejected for binding).

## Config
- Subscription (`fleet_launch.opencode_subscription()`): service `go`, engine `opencode`, account_label as configured, admission_argv = maintained fresh gate, launcher_provider `opencode`, launcher_model `opencode-go/muse-spark-1.3-contributor`. No nested `launcher` key (load_settings rejects it).
- Top-level launcher (`fleet_launch.opencode_launcher_config()`): {argv:[python3 -m launcher], root:<launcher checkout>, config_dir:<launcher config>}. Compose both with `fleet_launch.opencode_settings()` (passes real load_settings validation).
- Explicit honest local account mapping: Hub account_label ↔ quse route (`go`) ↔ launcher provider/model (`opencode`/muse-spark-1.3) ↔ admission gate. The maintained `plan` never carries Hub accounts (verified live 2026-10-17: eligible entries are {name, provider, model, health, remaining_fraction, hours_to_reset, task_fit, promo_multiplier}); account/profile stay bound by exact local subscription match (_route) + fresh gate account_label. Nothing is invented.
- Offer: mode headless, engine opencode, workspace label configured, prompt = arbitrary brief (verbatim, separate argv downstream, no shell interpolation), optional task_id for report/file outcome via `.local/hub-results/<offer>.json`.
- Launcher side (see launcher docs/opencode-muse-brief.md): quse go windows + `opencode models` containing exact model + runtime binary; `plan` must list an eligible opencode/muse-spark entry with task_fit 1.0.

## Binding
- Launcher aplexer backend prints JSON receipt with session_id (native UUID), engine opencode, first_action_verified true on success. Fleet dispatch returns `{state: running, native_id: <UUID>, launcher_task_id: hub-<offer>}` and POSTs `/api/fleet/launches/<offer>/result`. For opencode, native_id comes from session_id ONLY (unit_name/worker_id never accepted).
- Automatic Hub session binding in `once()` (`find_binding_row`): matches expected tags `hub-<fleet_launch_id>` AND `task-hub-<fleet_launch_id>` (actual launcher run_tag_for output) with engine match, requires row workspace + integer native creation evidence, and prefers the exact canonical server launch native_id when the pending entry carries one. A refused binding POST never kills the cycle (reconciled next cycle).
- Server seam requested from head (cleanup owns src/session_control.ts, NOT edited here): applyBinding demands tag `hub-<launch.id>` so `task-hub-` bindings 409 today; minimal seam is accepting `task-hub-<launch.id>` alongside plus exposing the launch native_id in GET /api/sessions/host-bindings entries (currently {id, fleet_launch_id, engine} only).
- Task-units `unit_name`-only receipts never validate as native opencode bindings (see test).

## Tests
- `host/tests/test_fleet_opencode.py` (9 tests): opencode headless uses aplexer backend (genuine plan shape, no task-units/--as-controller), others preserve task-units, unit-only receipt rejected / UUID receipt accepted, helper contract exact incl. real load_settings validation, plus 5 binding regressions (actual launcher `task-hub-` tag binds, `hub-` still binds, canonical native_id preferred over ambiguous tag-only, unproven rows skipped, refused binding never kills cycle).
- `host/tests/test_fleet.py`: finite-cycle expectation now includes `/api/sessions/host-bindings` (was failing on main; fixed in scope, green); headless plan refusal case uses wrong-model (honest contract).
