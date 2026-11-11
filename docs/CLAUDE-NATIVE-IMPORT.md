# Import an existing native Claude login

On an enrolled, approved host that already has a working native Claude login:

```bash
hub engines import-native --provider claude --label hub-claude
hub engines list
```

The operator must have Hub admin rights. The operator and enrolled host profiles
must target the same HTTPS Hub. The command reads `CLAUDE_CONFIG_DIR/.credentials.json`
when configured, otherwise `~/.claude/.credentials.json`. The file must be private
and owned by the current user. Symlinks, hard links, expired access tokens and
managed-engine homes are refused. It never opens a browser or starts a login.

For host sync, the command prefers the existing Hub2 enrollment at
`~/.config/agent-hub/v2.json` (`server`, `token`, `machine_id`). It reads the
enrollment through the private, bounded, no-follow file reader without modifying
it. On POSIX the enrollment directory must be owned by the current user with
mode 700 and the file private with mode 600. An existing unsafe, malformed,
incomplete or different-origin Hub2 enrollment stops the command before upload;
it never falls back to the legacy profile in that case. Only when Hub2 enrollment
is absent may a valid legacy `host.json` (`base_url`, `access_token`, `machine_id`)
be used. A stale legacy origin does not override valid Hub2 enrollment.

An explicit `HUB_CONFIG_FILE` selects the enrollment override; it must be an
absolute path to a valid host profile. Human and worker profiles are refused as
host enrollment. Inherited `HUB2_CONFIG`, `HUB2_SERVER` and `HUB2_TOKEN` never
select or replace the host enrollment. The existing operator uploader retains its
own authentication and URL selection. Host sync pins the validated enrollment
URL and token for this invocation, sends them only to the engine sync, Claude
registration and sync-report endpoints, and refuses all HTTP redirects. No token
is copied to another profile or included in command output.

Only the native Claude subscription OAuth record is uploaded. MCP credentials,
cookies, settings and browser profiles are excluded. The Hub seals the credential
using the existing account credential store and advances its generation with a
compare-and-swap check. A concurrent refresh or import refuses the stale write.
The native source file is never modified.

An older native access expiry normally refuses replacement of a fresher central
credential. Recovery from recorded central authentication or refresh failure also
requires the uploaded native credential to pass a live provider authentication
probe. This probe does not clear an authentication incident or prove Opus health.

After upload, the command reconciles the managed Claude installation and verifies
the enrolled host has acknowledged the current generation. It refuses a sync
response older than the import and ignores pending login work. Output contains
only the account label, provider, machine and generation numbers.

If acknowledgement fails after upload, the central import remains saved; inspect
`hub engines list` before retrying. A saved import or acknowledgement does not prove
provider health: acceptance requires a real managed Claude Opus invocation with a
harmless prompt through the normal quota-gated launch path.
