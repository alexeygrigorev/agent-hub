# Cloudflare Worker Deployment Probe Adapter (M14c Genuine Deployment Evidence)

This document specifies the operator-allowlisted native Cloudflare Worker deployment probe adapter in `host/agent_hub_host/status_probe.py`, integrated into the host status runtime (`host/agent_hub_host/status_runtime.py`).

## 1. Overview & Problem Solved

Previously, deployment evidence relied solely on argv-based probes (`wrangler deployments list`) with a loose substring matcher (`sha in json.dumps(item)`). That had two primary gaps:
1. Historical commit mentions or revert notes containing a target commit SHA could trigger false positive `deployed` assertions.
2. `wrangler deployments list` does not provide the active Cloudflare Worker version's exact `source_commit` annotation contract.

The native `cloudflare_worker` deployment adapter resolves these gaps by querying Cloudflare's read-only Workers API over fixed HTTPS endpoints, inspecting the latest deployment record, resolving active version annotations, and verifying the exact `source_commit` for all versions receiving positive traffic.

## 2. Security Boundaries & Invariants

1. **Fixed HTTPS Cloudflare API Origin**:
   All Cloudflare Worker API requests use the hardcoded origin `https://api.cloudflare.com/client/v4/`. Arbitrary URLs or hostnames in probe specs are rejected.
2. **Local-Only Private Credentials**:
   - Credential files are specified only in trusted local operator allowlists (`credentials_file`).
   - The file must be a regular file (`S_ISREG`), owned by the current user (`st_uid == os.getuid()`), and have strict permissions `0600` (`(st_mode & 0o777) == 0o600`).
   - Symlinks (`islink`) and broad permissions (`0644`, `0660`, `0777`) are strictly refused.
   - Only `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` are parsed. No environment or argv token copying occurs.
   - Credentials and credential file paths are NEVER sent to the Hub.
3. **No Redirects Forwarding Authorization**:
   HTTP redirects (301, 302, 303, 307, 308) are disallowed by a dedicated `DisallowRedirectHandler` to guarantee `Authorization: Bearer <token>` is never forwarded.
4. **Bounded Execution & Sanitized Diagnostics**:
   - HTTP timeouts are clamped to `1..120s` (default 15s).
   - Response bodies are capped at 1 MiB (`MAX_CF_BODY = 1048576`). Exceeding responses fail closed with `response_bounds_exceeded`.
   - Credentials and tokens are redacted from all error messages and diagnostic provenance.
5. **Fail-Closed Semantics**:
   Network errors, HTTP errors (401, 429, 500), credential errors, and unrecognized versions produce state `unknown`, NEVER `not_deployed`.

## 3. Deployment Evidence Contract

For a requested commit SHA:
- **`deployed`**: Full active traffic (100% of traffic) names the requested SHA in the active version's `source_commit` annotation.
- **`not_deployed`**: All active versions receiving traffic have valid `source_commit` annotations, and NONE of them match the requested SHA.
- **`unknown`**:
  - Mixed / canary traffic (e.g. 90% SHA A, 10% SHA B).
  - Missing or malformed `source_commit` annotations on any active version.
  - Invalid traffic percentages (sum != 100 or negative).
  - Network timeouts, HTTP 401, HTTP 429, or unparseable JSON.

### Structured Matching Regression Fix
In `build_deployment`, loose string search (`sha in json.dumps(item)`) has been replaced by `_extract_deployment_shas(item)`. Structured commit keys (`sha`, `source_commit`, `commit_sha`, `head_sha`) are extracted from records; arbitrary strings in `note`, `message`, or `description` containing the target SHA are ignored.

### Snapshot Caching within Collection Call
Within each `collect_probes` invocation, deployment and version metadata are cached by `(account_id, script_name, credentials_path)`. Multiple sessions or probes querying the same script in one collection round reuse the snapshot, avoiding duplicate API calls.

## 4. Operator Allowlist Configuration

In the host's `status-allowlist.json`:

```json
{
  "machine_id": "hetzner-host-1",
  "repo_roots": ["/home/alexey/git/agent-hub"],
  "probes": [
    {
      "id": "cf-hub-production",
      "kind": "deployment",
      "adapter": "cloudflare_worker",
      "for_native_id": "native-session-uuid",
      "sha": "6aa7a5542b260ba3065a7e58a2e55ef232da1e71",
      "script_name": "agent-hub",
      "environment": "production",
      "credentials_file": "/home/alexey/.config/cloudflare/agent-branches.env",
      "timeout_s": 15
    }
  ]
}
```

Optional Python helper available in `status_probe.py`:
```python
from agent_hub_host.status_probe import make_cloudflare_probe

probe = make_cloudflare_probe(
    probe_id="cf-hub-production",
    for_native_id="native-session-uuid",
    sha="6aa7a5542b260ba3065a7e58a2e55ef232da1e71",
    credentials_file="/home/alexey/.config/cloudflare/agent-branches.env",
    script_name="agent-hub",
    environment="production",
    timeout_s=15
)
```

## 5. Hub Observation Payload

Observations emitted for kind `probe_deploy` conform strictly to the Hub server-side schema (`state`, `sha`, `deployment`):

```json
{
  "machine_id": "hetzner-host-1",
  "native_id": "native-session-uuid",
  "kind": "probe_deploy",
  "observed_at": "2026-10-17T20:45:00.000Z",
  "payload": {
    "state": "deployed",
    "sha": "6aa7a5542b260ba3065a7e58a2e55ef232da1e71",
    "deployment": "dep-uuid-1 (version 11111111-2222-3333-4444-555555555555, 100% traffic)"
  }
}
```
No tokens, account IDs, or private credentials are included in the payload.
