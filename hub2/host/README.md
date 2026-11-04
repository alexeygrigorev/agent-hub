# Hub 2 host bridge

Python 3.11+. The host uses one pinned runtime dependency, `websockets==16.0`,
for the maintained TLS/framing implementation. Model admission, OS resource
locking and native lifecycle verification remain in Agent Quota Launcher.

Run `python hub2/host/bootstrap.py` to install the host into its private user
environment; bootstrap uses the current interpreter and installs dependencies.
It supports Windows and Linux. The `hub2` entry point provides enrollment,
serving, typed repository/intake/session/message/quota/schedule commands and a
bounded `request METHOD /api/path --body-file input.json` escape hatch.

Human CLI authentication uses `hub2 --server https://hub.example auth login`.
It opens a public device approval page, polls the one-use device code and saves
private access/refresh credentials. `--no-browser` prints only the approval URL
and user code. Expiring CLI access is refreshed before requests; `auth refresh`
and `auth logout` perform server rotation/revocation. A lost refresh reply stays
uncertain rather than replaying a single-use refresh token.

Machine enrollment takes a one-use code from `HUB2_PAIRING_CODE` or a hidden
prompt. Its private configuration records the enrolled machine, workspace roots
and engine allowlist. The trusted installation registry supplies the maintained
launcher paths, resource policy and configured provider routes; tasks cannot
choose executable commands or credential homes. For example:

```json
{
  "launcher": {
    "argv": ["/installed/python", "-m", "launcher"],
    "repository": "/installed/agent-quota-launcher",
    "config_dir": "/private/launcher-state",
    "resource_config": "/private/resource-policy.json",
    "capabilities_manifest": "/private/native-capabilities.json",
    "routes": {
      "opencode": {
        "provider": "go",
        "model_requirements": {"providers": ["opencode"]}
      },
      "codex": {
        "provider": "zai",
        "account_profile": "hub-zai-central",
        "model_requirements": {"providers": ["zai"]}
      }
    }
  }
}
```

Routing policy supplies the account and exact native model. A human route ID
such as `glm-flash` is distinct from its configured SDK model. The only ZAI
account is `zai-central`, through the Codex harness profile `hub-zai-central`.
There is no retired-account fallback or legacy Hub host-package import.

Before spawning, the host downloads its approved Account generation, atomically
materializes a private SDK home, verifies the SDK-selected credential source
and acknowledges that exact generation. Ordinary OAuth homes contain access
material only; shared refresh tokens remain central. Actual access expiry and
the verified SDK proactive-refresh cutoff must exceed the bounded job timeout
plus clock/dispatch margin. Unknown expiry or an unsupported SDK policy refuses
admission. Static ZAI credentials do not use OAuth refresh permits.

The launcher repeats authenticated session/account/generation/quota checks in
its parent process and keeps its OS resource admission lock. The private parent
controller config never enters native argv, environment, task brief or Store
payload. Native metadata receives only the provider-home path and a separate
private worker config path. The worker token can read/append its assigned intake
and use its own claims/mailbox; it cannot start work or edit project/admin state.
Verified completion durably revokes it before forwarding finish.

Capabilities use manifest version 3 with native_sha256 bound to the resolved native aplexer executable, plus SHA-256 bindings for exactly
`launcher/launch.py`, `launcher/cli.py`, `launcher/interactive.py` and
`launcher/provider_home.py`. Per-provider capabilities explicitly include
`headless`, `interactive`, `effort` or `goal`. Missing/changed source removes
advertisement; a goal capability requires real proof for that provider/account
route. Evidence from a different account does not qualify.

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
