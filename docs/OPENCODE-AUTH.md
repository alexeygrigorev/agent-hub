# OpenCode device login on Hub hosts

OpenCode v2 stores current credentials in its SQLite database. Legacy
`auth.json` is a migration input, not the current device-login source.
Importing that JSON into an isolated home resurrects stale Go API credentials.
Hub uses the native Console device login for the pinned subscribed
`opencode-go/muse-spark-1.3-contributor` route. No API key is required.

Configure the trusted machine-local `subscriptions.json` with:

```json
{
  "opencode_auth": {
    "binary": "/absolute/path/to/opencode",
    "source_data": "/native/xdg-data",
    "source_config": "/native/xdg-config",
    "target_data": "/hub/private/opencode-device-data"
  }
}
```

Use a new empty, owner-only target directory. Preserve old data directories and
back them up privately; never delete legacy credentials as part of migration.
Log in on the source using `opencode auth login opencode --method device`.
The source and target must belong to the same authorized account on this host.
Credentials never transit Hub, messages, reports or command-line arguments.

Before dispatch, Hub uses native `auth export opencode --standalone` privately,
selects exactly one active Console device credential, and imports only that
credential into the dedicated shared runtime. Every launch reads the live source.
An owner-only source fingerprint prevents unchanged source snapshots from
replacing newer native runtime refreshes. A new native login is imported at the
next check. Native `auth list` rejects any key-contaminated runtime, and native
`api model.list` must expose the enabled pinned Muse model. No inference is
performed by this preflight; the actual launch still passes fresh quota and
resource admission in the maintained launcher.

The dispatcher applies the same database/data/config environment to the launcher
and its native children, replacing nested old data overrides and stripping
OpenCode API-key/config overrides. Managed Go bundles accept no files or env;
the managed home obtains its local device configuration from the same host file.
No launcher or aplexer runtime implementation is replaced.

An auth preflight failure occurs before claim or native startup and is reported
as `engine_auth_expired`. Hub records the refused attempt as `not_started`, holds
the affected route and queues the original request for another eligible engine
in its tier. Explicit engine constraints remain queued with an auth rejection.
The fenced reject endpoint refuses to retire a claimed launch. Subsequent native
auth failures retain existing correlated-log classification and cleanup proof.
Host polling repeats the device check; its success clears only this machine's
auth hold, never an unrelated execution-health hold. A successful model response
and a confirmed native finish remain separate acceptance evidence.

Native storage contract: [OpenCode v2 providers](https://dev.opencode.ai/v2/docs/cli/providers).
