# Hub 2 host bridge

Python 3.11+. The host uses one pinned runtime dependency, `websockets==16.0`,
for TLS and framing. Hub selects a quota-weighted model route and enforces one
global limit of 50 live sessions. The host launches the accepted work through
aplexer, reports pane activity, and handles confirmed stops; it does not run
machine-capacity or placement checks.

Run `python hub2/host/bootstrap.py` to install the host into its private user
environment; bootstrap uses the current interpreter and reinstalls the current
package so an update replaces an already installed release. It supports Windows
and Linux. Enrollment and CLI requests default to `https://hub.dtcdev.click`;
`--server` selects another Hub explicitly. The `hub2` entry point provides
enrollment, serving, typed repository/intake/session/message/quota/schedule
commands and a bounded `request METHOD /api/path --body-file input.json` escape
hatch.

Human CLI authentication uses `hub2 --server https://hub.example auth login`.
It opens a public device approval page, polls the one-use device code and saves
private access/refresh credentials. `--no-browser` prints only the approval URL
and user code. Expiring CLI access is refreshed before requests; `auth refresh`
and `auth logout` perform server rotation/revocation. A lost refresh reply stays
uncertain rather than replaying a single-use refresh token.

Machine enrollment takes a one-use code from `HUB2_PAIRING_CODE` or a hidden
prompt. Its private configuration records the enrolled machine, workspace roots,
engine allowlist and installed provider routes. Hub policy supplies the allowed
account, exact native model and effort; tasks cannot choose executable commands
or credential homes. Model policy excludes `zai-central`, Sol/Argon/Astra high,
Claude models other than Sonnet medium, and Luna models other than max.

Before spawning, the host downloads its approved Account generation, atomically
materializes a private SDK home, verifies the selected credential source and
acknowledges that generation. Ordinary OAuth homes contain access material only;
shared refresh tokens remain central. Authentication failure is reported to Hub
so it can try another allowed route; quota and host-resource checks do not refuse
the start.

The private parent configuration never enters native argv, environment, task
brief or Store payload. Native metadata receives only the provider-home path and
a separate private worker config path. The worker token can read/append its
assigned intake and use its own claims/mailbox; it cannot start work or edit
project/admin state. Verified completion durably revokes it before forwarding
finish.

`hub2 --config private/v2.json serve` maintains an authenticated WebSocket with
bounded frames, no idle application polling or pings. Reconnection drains
durable offers, commands and mail. Local launch/command intents precede native
effects; ambiguous effects are never blindly retried. Unix modes and Windows
ACLs protect configuration and journals. Cwd and PWD both bind the assigned
workspace. Native first-action, model, effort, goal and containment/exit receipts
are verified independently. Exit zero records evidence for review; it does not
accept the business outcome.

Run focused host tests with `PYTHONPATH=hub2/host python -m unittest discover
-s hub2/host/tests` after installing the host dependency. Provider/native test
fixtures explicitly distinguish synthetic receipts, actual SDK parsing and real
provider/model calls.
\nAll Hub child launches pass the immutable server-issued worker credential deadline to native aplexer. The maintained launcher removes ambient APLEXER_WORKER overrides so the reviewed native CLI reexecutes itself. Native deadline evidence and the actual exit timestamp are mandatory for new completion receipts; a timeout preserves its actual exit code and is failed, never accepted work.\n
A verified HTTP 429 budget_paused response carries retry_at in Unix milliseconds. The host pauses normal heartbeat/connect/offer work until that bounded deadline while continuing local native reconciliation and qualified finish delivery. An authenticated stop-only WebSocket uses /api/connect?safety=operator_stop; only stop commands and their marked ACKs pass during the pause. Finish and stop ACK receipts bypass paused ordinary acknowledgement rows. An exhausted safety reserve defers remote reconnect/flush to its bounded retry deadline while native hard deadlines remain active.

Install native release binaries under the canonical private filename aplexer (aplexer.exe on Windows). Capability verification resolves symlinks and refuses other basenames: v0.1.25 portable asset filenames can select an older PATH worker. Both native deadline canaries used the exact installed canonical binary; an isolated upstream fix owns portable filenames.

Native signal termination keeps raw exit evidence unchanged (code null and signal 1?127). The wire returncode is -signal; genuine signed 32-bit Windows exit codes are preserved. Contradictory code/signal evidence never becomes successful completion.
