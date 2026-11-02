# shared-config/ orphan: decision (audit e2501fde, intake fc1ddc7a)

## Decision

**Remove the orphaned `shared-config/` data from main.** Do not land the
missing code from here.

## Evidence (2026-10-31)

- `shared-config/` (`README.md`, `manifest.json`, `aws/config`) came from data-only
  commit `9afe7b4e` ("feat: ship Gate MAIN profile in shared AWS bundle").
- Its README documents `host/agent_hub_host/aws_gate.py`,
  `host/agent_hub_host/shared_config.py`, `host/tests/test_shared_config.py`,
  `hub shared-config sync` and `hub aws-gate credential-process`. None of these
  exist on main.
- The README also says `shared-config` is in `scripts/build-host-release.py`
  `TRACKED_PATHS` and `scripts/host-release-manifest.json` `hashed_trees`. It is
  in neither, so the bundle is never shipped to hosts and no code reads it.
- The code commits the audit names (`9fff48dc`, `b477027b`, `a104fa92`,
  `23d03685`) are missing from GitHub. `gh api repos/alexeygrigorev/agent-hub/commits/<sha>`
  returns HTTP 422 "No commit found" for all four. They are also missing from
  every remote branch (`main`, `agent-hub-wt-*`) after a full fetch.
  Without that source the feature can't be landed or reviewed from main.
- The `aws/config` entry runs `credential_process = hub aws-gate credential-process`.
  On main that command doesn't exist, so the documented behaviour can't work as
  shipped.

Keeping data whose documented consumer and command are absent misleads
operators. Removing it changes nothing at runtime, because nothing on main
reads it. No AWS resources, credentials or host files were touched.

## Restoring it later

If whoever holds the unmerged AWS Gate commits (`aws_gate.py`, `shared_config.py`,
their tests, CLI wiring and release-manifest entries) lands them, restore the data
in the same feature with `git checkout 9afe7b4e -- shared-config`. Then add
`shared-config` to the host-release tracked paths and run the full suite.
