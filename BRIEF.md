# Brief: Fix Hub CLI human authentication and routes on Hub 2 (Hetzner / box)

## Context
Developer Gmail CI watch reported at 12:02 UTC:
After successful `hub auth login` (`~/.config/agent-hub/cli.json` updated with human access_token, admin scope), CLI calls against `https://hub.dtcdev.click` fail:
- `hub auth status` -> Status operation failed (`GET /api/auth/me` returns 404)
- `hub quota` -> Hub quota operation failed (`GET /api/quota` returns 404)
- `hub sessions list` -> "No sessions found" (verify projection & machine registration)
- `hub repos list` -> `{"items": [], "next": null}` (verify repo list/import compatibility)
- Box CLI autoupdate manifest -> HTTP 401

## Objectives & Acceptance Criteria
1. **Fix `hub auth status`**:
   - Add `/api/auth/me` endpoint in Hub 2 Worker (`hub2/src/worker.ts` and `hub2/src/control-auth.ts` / `hub2/src/auth.ts`) so authenticated human requests (Bearer token or cookie) return actor profile (e.g. `{ "actor": "human:...", "email": "...", "sub": "...", "label": "...", "scopes": [...] }`).
   - Verify locally with curl and `hub auth status` that it prints `Status: Logged in as human` and displays user/admin details.

2. **Fix `hub quota` route**:
   - Provide route for `GET /api/quota?view=fleet` (or map to Hub2 account/fleet quota projection) so `hub quota` client in `host/agent_hub_host/quota.py` succeeds rather than returning 404.

3. **Check sessions & repos projection**:
   - Ensure `hub sessions list` and `hub repos list` wire contracts match CLI expectations on Hub 2.
   - Investigate box CLI autoupdate manifest 401 and fix authentication/permissions if route exists.

4. **Testing & PR workflow**:
   - Run targeted unit tests for touched files only.
   - Push branch `fix/cli-auth-routes` to `origin`.
   - Open PR via `gh pr create`.
   - Verify CI passes on GitHub Actions, then merge (`gh pr merge --auto --rebase`).
   - Once CLI is functional, create an intake for tracking if appropriate.
