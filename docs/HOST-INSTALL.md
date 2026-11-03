# Install a Hub session host

Run the command for your OS in your ordinary user account. The installer uses
per-user paths and does not request administrator access, `sudo`, or a GitHub
login.

**Windows PowerShell**

```powershell
irm https://hub.dtcdev.click/host/install.ps1 | iex
```

**Linux or macOS**

```bash
curl -fsSL https://hub.dtcdev.click/host/install.sh | sh
```

The installer downloads its runtime packages, configures the host and displays
one Hub device URL and code. Approve that request in your browser. The approval
covers the host credential and requests repository-read scope; repositories
are cloneable when the Hub operator has enabled the repository policy. No
separate GitHub login is used. ARM64 is detected automatically, with
x64-compatible runtimes used where a native package is unavailable.

The installation is repeatable: rerun the same command after an interruption
to resume setup. Existing credentials and repository work are preserved. The
installer ends with JSON containing the machine ID, service type, elevation
prompt count and repository-access state. `repository_access: configured`
means approved repositories can be cloned; `not_configured` means the Hub
operator must enable repository access; `declined` or `forbidden` means access
was not granted; `unavailable` means retry after a temporary outage.

The Windows controller runs as a per-user logon task and updates automatically when Hub serves a new `hub.pyz` (spaced by 12 hours, or immediately when requested via `hub host-release --machine <id> --sha <sha>`; see [HOST-RELEASE.md](HOST-RELEASE.md#windows-package-installs-immutable-release-trees-supported-update-path)). Linux uses a systemd user service. Keep your user session active so the controller can report availability. macOS installs per-user files, but its controller currently does not start because required resource limits cannot be enforced there. Fresh Windows-profile end-to-end verification is still pending.
