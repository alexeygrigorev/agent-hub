# Host resource and workspace advertisement

Hub2 hosts derive engine capabilities from installed native providers. Resource
tags describe operator-verified tools on that host; they do not authorize using
a browser, phone, account, or service. Existing approval requirements still apply.

The release environment's native CLI can configure an existing enrollment:

```powershell
python -m agent_hub_host.cli --config C:\path\to\v2.json configure-host `
  --capability browser:orin-chrome --capability tool:agent-browser `
  --workspace "home=C:\actual\enrolled\directory"
```

Use the installed release environment's Python and actual private configuration
path. The directory must already exist under an enrolled `roots` directory.
`configure-host` replaces all configured resource tags and aliases, preserves
enrollment credentials and launcher settings, and makes no network requests.
Running it without resource or workspace options clears those observations.
It never creates directories, expands enrolled roots, or installs a provider.

The private configuration fields are:

```json
{
  "capabilities": ["browser:orin-chrome", "tool:agent-browser"],
  "workspaces": {"home": "C:\\actual\\enrolled\\directory"}
}
```

Resource tags accept only `browser:`, `tool:`, and `phone:` namespaces. Configure
only resources verified by their owner; do not infer tools from the machine name
or advertise `phone:adb` / `tool:smscli` without checking their availability.
Engine, credential, and approval markers cannot be supplied through this command.
Installing a host release alone does not configure these resource observations.

The daemon loads configuration at startup. Its owner must coordinate any reload
or restart with active sessions. A standalone `heartbeat` reads the configuration
but does not reload an already running daemon; its next heartbeat may overwrite
that report. Do not use a one-shot heartbeat as proof of sustained acceptance.

Aliases resolve without regard to case and publish as `workspace:<lowercase-label>`.
Aliases take precedence over basename lookup. Both launch resolution and heartbeat
recheck existence and root containment; an unavailable or escaped alias is omitted
from heartbeat and refused at launch. Unconfigured labels retain legacy basename
resolution but have no explicit advertisement. There is no implicit `home` alias.

Routing prefers a single engine/host pair satisfying all required capabilities
and the workspace label. `os:windows` is matched against the host's reported OS.
Absolute paths retain path-family handling and need no workspace label tag.
When no pair advertises the complete request, the existing advisory fallback
remains visible as `capability_not_advertised`; it is not a promise of success.
The host still enforces its own installed-provider and workspace checks.

For coordinated desktop acceptance, the desktop owner must verify the resources,
configure the correct enrolled path, and safely load the accepted release/config.
Then capture a sustained live `hub routing-machines` read showing the connected
desktop's `browser:orin-chrome`, `tool:agent-browser`, and `workspace:home` tags.
An `--engine antigravity` request additionally requires the actual provider to be
installed, configured, and advertised. Do not retry an unrelated failed desktop
task to test this repair, and do not access its browser without an approved grant.

The retired `gate.dtcdev.click` is unrelated to these observations. Do not restore
its DNS, Caddy listener, CLI, or approval flow. A synthetic box resolver mapping
to `198.18.0.1` must be removed by the box/network resolver owner, with a fresh
box-side lookup and connection check proving the dead TLS interception is gone.
