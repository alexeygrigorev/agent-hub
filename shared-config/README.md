# Shared configuration status

The shared-config bundle is not an installed Hub feature. Intake
`fc1ddc7a-3194-4959-ba70-d3f5c9e38643` removes the orphaned `manifest.json`
and `aws/config` from the source tree. No host configuration, AWS resource,
or credential is changed by this removal.

The data landed in `9afe7b4e3883f182ebbe4ee8c3e49a4ae920f9b4`, but its
consumers did not land on main: `host/agent_hub_host/aws_gate.py`,
`shared_config.py`, `host/tests/test_shared_config.py`, and the
`hub shared-config sync` / `hub aws-gate credential-process` commands are
absent. The current CLI builder, host-release tracked paths and release
manifest also do not distribute this bundle. The previous README described
those missing integrations as installed behavior.

The implementation is preserved in Git objects `9fff48dc` / `b477027b`
(duplicate feature versions), with follow-up `a104fa92` / `23d03685`
(duplicate fixes for independence from host credential overrides). Restoring
it would add credential-process HTTP requests, host configuration writes,
managed-engine AWS environment changes, and release packaging. Those
changes need their own complete implementation and verification; they are
unnecessary to resolve this audit's orphaned-data defect. The data-only
commit also added an optional MAIN-account role, which is not needed here.

Decision: remove the nonfunctional configuration payload rather than land
that broader credential integration as an audit repair. The original data
and implementation remain recoverable at the commits above. A future
shared-config feature must ship its consumer code, packaging and tests
together, document its actual installed state, and preserve the operator's
AWS access restrictions. This repair uses no AWS access or secrets.
