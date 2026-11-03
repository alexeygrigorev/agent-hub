# Isolated test Hub for host enrollments

Tests and installer E2Es must use an isolated Hub. Their host configuration
must point at that test Hub before the installer starts. The shared host HTTP
transport rejects enrollment, machine registration, host heartbeats, and route
advertisements to production whenever pytest, the hermetic test harness, or an
explicit installer E2E is running. The guard applies even when a test-marked
credential is present; test machines stay entirely out of the production
registry.

## Installer E2Es

Set `HUB_E2E=1` for the entire test process tree and set `HUB_BASE_URL` to the
isolated Hub before running the one-command host installer. This arms the
guard in the installer and in the host process it starts. A test may download
the installer script from a production-served URL while keeping
`HUB_BASE_URL` pointed at the isolated Hub.

The Docker lifecycle test uses `TEST_HUB_URL` (loopback by default), rejects a
production URL before making requests, and passes both environment variables
to its installed child process. Its temporary Hub owns all pairing and
machine records.

## Guard coverage

The guard lives in the common HTTP transport, so calls from registration,
device pairing, host check-in, route advertisement, and fleet helpers share
the same check. It recognizes both the production custom domain and the
production Workers URL. Read-only requests and nonproduction test Hubs remain
available. A source scan in the host test suite flags tests that combine a
production Hub address with a machine-registry driver; the transport test
proves that the run fails before a request reaches the network.
