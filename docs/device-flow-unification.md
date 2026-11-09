# Device flow comparison

Evidence inspected on 9 November 2026 from Hub `fix/hub-unified-device-flow`,
DataTalksClub/auth `main` at `90f415b0e54a05a5bb9c122f0c567107c5e4f078`,
and the local dapier and DataOps checkouts. This records the pre-change behavior
and the Hub code-format change made in this branch.

| App | User code | Start and poll endpoints | Approval page |
| --- | --- | --- | --- |
| Hub 1 (`hub auth login`) | Before this branch: 8 characters, `4-4` split, alphabet `ABCDEFGHJKMNPQRSTUVWXYZ23456789` in `src/auth/crypto.ts`. Updated here to the shared alphabet `BCDFGHJKLMNPQRSTVWXZ23456789`, retaining the shared `XXXX-XXXX` format. | `POST /api/device/code` and `POST /api/device/token`; Hub owns the pairing records and issues its own human credentials (`src/hub.ts`, `host/agent_hub_host/human_auth.py`). | Hub `/device?code=...`, rendered through `authPage()` and the shared `dakit` auth shell (`src/ui-pages.ts`). It reviews the human login label, code, expiry and admin grant. |
| dapier (`dapier auth login`) | 8 characters, `4-4` split, `USER_CODE_ALPHABET` from `src/dapier/auth/device_sessions.py`; the dakit canonical `BCDFGHJKMNPQRSTWXZ23456789` (no L/V, mirrored by `src/web/vendor/device-code.js`, which the `/device` page validates against). Deliberately two characters narrower than the shared issuer alphabet; same `XXXX-XXXX` format. | `POST /api/agent/device/start` and `POST /api/agent/device/token`; dapier owns pairing and device sessions (`src/dapier/api/agent/device.py`). | `/device?code=...`, dapier's confirmation page (`src/web/device.html`, `src/web/js/device.js`), with identity and request facts before approval. |
| DataOps (`dataops login`) | Issuer-generated 8-character code in `XXXX-XXXX` form from DataTalksClub/auth `src/oauth_server.py` (`DEVICE_CODE_ALPHABET`). | `POST /oauth/device` and `POST /oauth/token` on the shared issuer. The CLI loads the shared `dtc_device_login/login.mjs` loop (`cli/src/commands/auth.mjs`). | Shared issuer `/device?code=...`, served by DataTalksClub/auth `handle_device_page_get()` in `src/oauth_server.py`. |

The Hub CLI's polling implementation is the stdlib-compatible
`dtc_device_login` source pinned in `host/agent_hub_host/_vendored_device_login.py`
and bundled with the standalone CLI by `scripts/build-hub-cli.py`. Hub mounts
the same device-grant contract at Hub-owned endpoints because approvals and
credential issuance remain Hub responsibilities. The shared helper handles
the code prompt, interval, pending/slow-down states, timeout, denial and
expiry; it does not create server codes or own the browser page.

The page implementations are app-owned because they show and approve each
app's own credential and permissions. Hub already uses the shared `dakit` auth
shell, but its text and review facts are Hub-specific. The implementation
change here aligns the Hub code alphabet and displayed code format with
dapier/DataOps while preserving Hub's approval authority and URL contract.

Net result: Hub and DataOps (shared issuer) now issue codes from exactly the
same alphabet. dapier keeps the dakit canonical alphabet, which intentionally
excludes L and V; Hub matches its `XXXX-XXXX` format, hand-typing UX and
ambiguity-resistant letter set, but does not narrow further because the shared
issuer page accepts the wider set.

## Source references

- Hub: `src/auth/crypto.ts`, `src/hub.ts` (`/api/device/code`,
  `/api/device/token`, `/device`), `src/ui-pages.ts`,
  `host/agent_hub_host/human_auth.py`.
- dapier: `src/dapier/auth/device_sessions.py`,
  `src/dapier/api/agent/device.py`, `src/web/device.html`,
  `src/web/js/device.js`.
- DataOps/shared auth: `cli/src/commands/auth.mjs`,
  `dtc_device_login/login.mjs`, `src/oauth_server.py`.
