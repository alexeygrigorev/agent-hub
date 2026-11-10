# Hub2 host releases

CI builds a host wheel and the full CLI zipapp from the same committed Git
snapshot with `python3 scripts/build-hub2-release.py --rev HEAD`. The output
directory is `hub2/public/cli`. The build excludes tracked `build/`, egg metadata
and bytecode. It embeds the full source commit and protocol version in
`agent_hub_host._release`. Both the pinned build backend and the pinned
`websockets==16.0` transport are installed only in isolated build/release
environments. Do not pip-install a worktree into the live host environment.

`host-release.json` has `schema_version: 1`, `host_version` (full Git SHA),
`protocol_version: 1` and `artifacts` mapping names to `sha256` and byte `size`.
The manifest is published last. HTTPS authenticates downloads; hashes detect
partial or mixed releases. Host heartbeat and routing-machine responses expose
the host version and protocol mismatch separately.

On Linux with Python 3.11+, curl, uv (or working stdlib venv/pip), aplexer and a running systemd user
manager, installation is:

```sh
curl -fsSL https://hub.dtcdev.click/cli/host-install.sh | sh
```

The installer prompts privately for a pairing code if no machine configuration
exists. `HUB2_PAIRING_CODE` supplies it noninteractively; it is never a command
argument. `HUB_WORKSPACE_ROOT` chooses the enrolled workspace root. The existing
central enrollment authority issues the machine token into the mode-600
`~/.config/agent-hub/v2.json`; no personal provider login is requested by this
installer. The release's portable `hub2-enroll.py` helper explicitly records
`launcher.private_provider_homes=true` for fresh nonlocal enrollment, retaining
the runtime's central credential provisioning path. Its `--local-profiles` flag
is reserved for hosts already configured with native logins. Enrollment must
advertise only engines the machine can actually launch; operators should set
the supported engine list in that private configuration before starting
production work. The helper is portable; Windows service/update automation
requires its separate platform adapter and live verification.

The host user unit runs `~/.local/share/agent-hub2/current/bin/hub2 serve`.
Its update timer checks every five minutes, downloads all artifacts, verifies
their hashes, builds a new environment under `releases/<commit>` offline from
the shipped wheels, checks the heartbeat signature, then switches `current`
atomically and restarts the daemon. It observes the service for 30 seconds and
requires an authenticated heartbeat before accepting activation. `previous`
retains the old environment; a failed activation switches back and restarts it.
Failed versions have persistent exponential retry backoff (up to six hours).
Interrupted installations carry an ownership marker and are recovered under
the updater lock; venvs are created at their final path to preserve shebangs.
Installed user units refresh with each release and restore on rollback. The
updater is syntax/help checked before self replacement. Retention keeps three
managed releases, including current and previous; unrelated directories are
preserved. Python requirements are checked before installation, and newer host
protocols are accepted only when the installed wheel reports the manifest's
protocol. No existing
environment, legacy CLI or unrelated service is replaced. The full CLI from
the same commit is `current/hub.pyz`.

The transport wheel is downloaded in CI with `--require-hashes` against the
reviewed root `uv.lock`; installs have no package-index access. Artifact trust
uses the existing authenticated HTTPS origin, with checksums for integrity;
this release format does not claim a detached signature. The Linux installer
enables user lingering so the host can run without an interactive login; the
OS must permit that operation for the installing user.

Live adoption belongs to the deployment owner. Migrating an existing hand-made
service requires that owner's acknowledged handoff; repository tooling does
not authorize a live service replacement. Enable the timer only after machine
enrollment. Check routing-machine version and run the start canary before and
after migration. A running systemd unit alone does not prove launch health.

CI workflow hooks, immutable artifact delivery, runtime version reporting,
canary rollback and desktop enrollment are coordinated changes; acceptance
requires their integrated evidence. Linux user units do not install a Windows
service or prove enrollment on an offline desktop.
