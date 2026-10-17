# Native Sessions Adoption & Qualified Addressing

## Overview

All native `aplexer` sessions on host machines (such as Hetzner) are observed through the maintained host catalog pipeline and adopted into the canonical Hub session control view. This provides full visibility and control (`list`, `show`, `send`, `capture`, `stop`, `commands`) over existing native sessions—including long-running background tasks, developer sessions, and psc/a workloads—without requiring duplicate Fleet launches, fake agent registry bindings, or artificial credential minting.

## Key Principles & Design

1. **Host-Attested Observation**:
   - The host's periodic poll cycle (`fleet.once`) invokes `aplexer list --json` and packages native metadata via `session_metadata()`.
   - The full catalog is reported to `PUT /api/fleet/sessions` (and optionally `POST /api/sessions/host-adoption`) without silent slice truncation (`[:200]` removed).
   - Server-side adoption (`adoptObservedNativeSessions`) matches the exact native UUID, tag, workspace, integer `created_at_ms`, and engine.
   - Machine-bound host credential checks ensure that only the enrolled machine can report observations or claim execution offers for its sessions.

2. **Qualified Native Aliases (`<workspace_basename>:<tag>`)**:
   - Every adopted session is accessible via its canonical UUID or its qualified native alias: `<workspace_basename>:<tag>`.
   - Examples:
     - Workspace `/home/alexey/git/dataops` with tag `batching` $\to$ `dataops:batching`
     - Workspace `/home/alexey/git` with tag `crm-redesign` $\to$ `git:crm-redesign`
   - **Resolution Rules**:
     - Exact match only.
     - Ambiguous matches (collisions across different directories with the same basename or historical reused tags) are refused honestly with `409 ambiguous_name` rather than assuming "latest wins".
     - In case of ambiguity, the canonical native UUID is always usable.
     - Friendly name validation (`--name` on `start`) is preserved and remains separate (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, strictly excluding colons).

3. **Provenance & Controllability**:
   - Each session in `GET /api/sessions` carries explicit provenance:
     - `provenance`: `'observed_native'` for adopted host sessions, `'canonical_hub'` for sessions initiated through Hub intents.
     - `controllability`: `'controllable'` for active/running sessions, `'historical_read_only'` for stopped/exited sessions.
     - `workspace`: Canonical absolute workspace path.
     - `qualified_alias`: Pre-computed `<workspace_basename>:<tag>` alias for client convenience.

4. **Security & Authorization**:
   - Adopted sessions are owned by `actor = 'unknown'` with null owner token IDs.
   - Authorized human admin devices pass authorization checks and can inspect, send prompts, capture screen/tail output, and stop adopted sessions.
   - Foreign agent credentials fail closed: requests by non-admin agent tokens to view or control adopted sessions receive `404 not_found` / `403 forbidden`.
   - Normal Hub-started sessions with associated `fleet_launch_id` are preserved and kept untouched.

5. **Lifecycle & Absence Semantics**:
   - Absence from a subsequent host poll snapshot is NOT proof of session exit. Missing sessions remain in their known state; they are never deleted or prematurely marked stopped on missing poll data alone.
   - Confirmed exit requires explicit terminal phase reports (`exited`, `dead`, `pruned`, `forgotten`) or verified stop acknowledgments with containment evidence.
   - Adopted native sessions are permanently protected from automated cleanup (`cleanup --apply`) sweeps.

6. **All Model Engines Supported**:
   - Native sessions running any engine (`codex`, `grok`, `gemini`, `opencode`, `argon`, `shell`, etc.) are recognized and controllable without launching new engine instances or modifying engine configurations.
   - Raw process environment variables, arguments, secrets, and private terminal output are stripped and never exposed in public listings.

## CLI Usage Examples

```bash
# List all sessions visible to human admin (including dataops:batching and git:crm-redesign)
hub sessions list

# Inspect session by qualified alias or UUID
hub sessions show dataops:batching
hub sessions show git:crm-redesign
hub sessions show 021d9370-9b3d-4e5a-b59b-256334c08c75

# Enqueue input prompt on an adopted session
hub sessions send dataops:batching --body-file /path/to/prompt.txt

# Capture tail or rendered screen output
hub sessions capture git:crm-redesign --screen

# Inspect command history
hub sessions commands dataops:batching

# Stop an adopted session
hub sessions stop git:crm-redesign
```
