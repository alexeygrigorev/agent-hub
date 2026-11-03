# Test-only code decisions — intake b6e2bef5

This records the owner and caller evidence used to resolve the test-only code
items in audit `e2501fde`. The current intake authorizes cleanup when a
candidate has no production caller and its tests are removed with it.

| Candidate | Evidence on `origin/main` | Decision |
|---|---|---|
| `task_authority.py`, `task_authority_adapter.py`, `oversight.py`, `oversight_adapter.py` | Production imports form a closed graph inside these four modules; only four `host/tests/test_{task_authority,task_authority_adapter,oversight,oversight_adapter}.py` files import them. The owner introduced them in `b75d1c8e` (“Add offline authenticated task grants and completion provenance”) and `6f34aa9c` (“Add bounded oversight intervention ledger”). The current Hub task/claim implementation is in `src/intake.ts`; the host-local prototype has no production consumer. | Delete the prototype cluster and its four test files. No production path changes. |
| `fleet_launch.py` | No production module imports it. The owner’s `79ce1297` Muse-brief design and `docs/fleet-opencode-muse-brief.md` describe it as a config/offer helper; current production dispatch is in `fleet.py`, and the remaining `test_fleet_headless.py` / `test_fleet_opencode.py` tests exercise that dispatcher directly. | Delete the uncalled helper and helper-only assertions; keep dispatcher integration tests. |
| `src/account-display.ts` | Its only caller is `test/usage-history.test.ts`. The production dashboard renders account labels in `public/usage-history.js`. The source helper was introduced with usage history commits `a1ce1824` / `2270116f` but was not wired into a production caller. | Delete the source helper and its isolated unit assertions; keep the live dashboard implementation. |
| `session_executor.start_from_offer`, its offer-target helpers, and `handle_session_command` | There are no production call sites outside `session_executor.py`. The `start_from_offer` compatibility comment predates the current dispatcher; `fleet.py` owns launch dispatch and `session_receiver.py` owns claimed send/capture/stop. | Delete the uncalled functions and their dedicated tests; retain the shared executor primitives and live receiver tests. |
| `session_lifecycle.validateCleanupTarget` / `validateOwnershipTransfer` and Python counterparts | There are no production call sites. The TypeScript `planCleanup` function is called by `session_lifecycle_registry.ts` and remains in place with its production tests. The review report `docs/reviews/SESSION-LIFECYCLE-R2.md` records the historical M15 guard work and the server’s authority boundary. | Delete only the unused validation seams and their tests; retain `planCleanup`, classification, schema, and live status behavior. |

The security-sensitive inline admin-token comparisons and other helpers in
`hub.ts`, `fleet.py`, `session_control.ts`, `intake.ts`, `resource-management.ts`,
and the public dashboard have active edit claims in other sessions. They were
left untouched under AC3 and remain outstanding for this intake. The same
applies to `worktree_guard.should_skip_cleanup`, which has an active edit claim;
its owner comment describes it as the shared cleanup predicate, so its intended
choice is to wire it into the production cleanup caller after that claim clears.
