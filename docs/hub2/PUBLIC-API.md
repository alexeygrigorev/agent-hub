# Hub2 public authentication and message compatibility

The Worker authenticates bearer requests through current Control credentials. Browser sessions use an opaque Secure, HttpOnly, SameSite=Lax cookie, server-side hashed credentials, current identity allowlists and an explicit CSRF token on mutations. An invalid bearer never falls back to cookie authority. Internal x-hub2 headers supplied by clients are removed.

## Sign-in

Configure AUTH_BASE_URL, AUTH_CLIENT_ID, AUTH_CALLBACK_URL, AUTH_ISSUER, AUTH_JWKS_URL and HUB_ADMINS (plus optional HUB_ADDITIONAL_ADMINS). DTC endpoints remain /oauth2/authorize and /oauth2/token under AUTH_BASE_URL. The callback origin must match the public origin. Authorization code exchange uses PKCE and one-use browser-bound state; ID tokens require a valid RSA signature, issuer, audience, nonce, expiry, token_use=id and allowlisted verified email.

GET /auth/login starts sign-in. GET /auth/session returns the current actor and CSRF token; POST /auth/logout revokes the browser credential. Browser mutations require same-origin checks and x-csrf-token. The dashboard connects automatically to an existing session and keeps manually entered bearer tokens in memory only.

CLI login uses POST /api/device/code with client_id. Open verification_uri_complete, approve the displayed code after browser sign-in, and poll POST /api/device/token with device_code at the returned interval (five seconds). Pending is HTTP428 authorization_pending; early polls are HTTP429 slow_down. Codes expire after ten minutes and issue credentials once. Access tokens expire after one hour. POST /api/token/refresh rotates both token hashes and revokes the old access/refresh pair; refresh has a thirty-day absolute lifetime. POST /api/token/revoke requires a currently valid bearer. Secrets must stay in private local storage, never command output or URLs.

Expired login, device and rate transport rows are cleaned on authentication requests in indexed batches of at most 25 per table, with no periodic idle work. Cleanup SQL is part of the measured workload.

## Messages and MCP

POST /mcp provides JSON-RPC initialize, ping, tools/list and tools/call under the same current authority. It does not issue credentials. Tools invoke actual REST handlers; typed API failures remain errors rather than fabricated success. File reads and secret issuance are not MCP tools.

The retained msg_send/reply/inbox/show/ack/status/accept/complete names map to the shared message handlers. to_actor maps to recipient, idempotency_key to key, and lease_epoch to epoch. from_actor cannot impersonate a human; choosing an agent requires its current family/client grant. Reply looks up the authorized parent and inherits its sender/thread through the owner. msg_show/inbox perform an explicit authorized epoch-bound read POST after the safe GET.

The current core message contract carries recipient, body, key, reply_to and epoch. Non-core kind/ref/deliver_by_s/thread_id options and non-inbox delivery requests currently return unsupported_message_option; they are not silently discarded. The old tool registry contains these options but is not evidence of active use. Any active consumer requiring them must be identified and migrated or the bounded owner contract extended before claiming compatibility. OAuth dynamic MCP client registration and the entire old generic automation registry are not asserted by this adapter.

Messages now use one physical global MessageDO. The authenticated mailbox identity remains per request and row authorization remains sender/current-recipient scoped; logical aliases are not separate storage shards. Message UUID/prefix resolution occurs in this authority rather than the Control message directory.

Native delivery authority is derived from the authenticated machine's exact persisted queue entry. The edge verifies message ID, operation, epoch, recipient and machine, then adds only that mailbox capability and the server-derived session ID to the internal request. Caller-supplied delivery/session headers do not grant authority. Native claims, physical delivery receipts and phone read/ack/accept/complete stages remain separate.

Mailbox and intake-list pagination use cursor; registry, intake history, and session owner pages use after. Fleet and global session projections use opaque bounded fan-out continuations. Intake/session UUID or unique prefixes resolve through the durable owner directory; no historical shard scan is used.

## Verification

Run node hub2/tests/run-normal-day-auth-workerd.mjs for the native SQLite/RSA/PKCE/device/rotation/revocation/CSRF/MCP alias contracts. Run HUB2_CORE_PUBLIC_MAIL=1 node hub2/tests/run-core-integration-workerd.mjs for the public five-owner native delivery and lifecycle contracts. These fixtures use explicitly synthetic provider/process evidence, not live provider or process claims. The normal-day gate fails until all canonical workload terms are exercised and their total stays below the architecture budget.
