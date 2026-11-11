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

Only the native Claude subscription OAuth record is uploaded. MCP credentials,
cookies, settings and browser profiles are excluded. The Hub seals the credential
using the existing account credential store and advances its generation with a
compare-and-swap check. A concurrent refresh or import refuses the stale write.
The native source file is never modified.

After upload, the command reconciles the managed Claude installation and verifies
the enrolled host has acknowledged the current generation. It refuses a sync
response older than the import and ignores pending login work. Output contains
only the account label, provider, machine and generation numbers.

If acknowledgement fails after upload, the central import remains saved; inspect
`hub engines list` before retrying. A saved import or acknowledgement does not prove
provider health: acceptance requires a real managed Claude Opus invocation with a
harmless prompt through the normal quota-gated launch path.
