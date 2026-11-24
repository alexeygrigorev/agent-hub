# Hub2 worker context: typed refusals and evidence reads

Hub-launched (`task-hub2-*`) sessions authenticate only with their per-attempt worker context
(`HUB2_CONFIG`, host-published). They never fall back to the human (`cli.json`) or host
(`host.json`) profile. Intake 37e34dcd made those refusals say why and gave fix workers a
read-only path to the evidence their intake names.

## Refusal reasons

Every refusal prints `Worker context refused (<reason>): <fixed text>` and exits non-zero. The text
never includes paths, tokens, URLs or other untrusted input; only the variable name from a fixed
list is interpolated.

| reason | fires when | does re-issuance help? |
|---|---|---|
| `worker_env_override_forbidden` | `HUB_CONFIG_FILE` or `HUB_WORKER_CONFIG_FILE` is set; `HUB_URL`/`HUB_BASE_URL` names another origin; `HUB_TOKEN`/`HUB_JOB_TOKEN` differs from the worker token. Checked first, so it is reported even if the context is also bad. | No. Unset the named variable. |
| `worker_profile_io_forbidden` | A worker command tries to read or write the human/host profile, or refresh a token. | No. |
| `context_missing` | No v2 launch identity or selector (`APLEXER_TAG`, `APLEXER_WORKSPACE`, `HUB2_CONFIG`), or the context file is absent. | Yes. |
| `context_expired` | The context deadline has passed. | Yes. |
| `context_invalid` | Malformed JSON, wrong fields, mismatched session, epoch, actor or server shape. | Yes. |
| `context_unsafe` | The file failed private-file checks (mode, owner, symlink, hard link). | Yes. |
| `context_changed` | The attempt was re-issued while the command ran. | No. Retry the command. |

Only the four `context_*` file conditions say "host re-issuance required".

## Evidence reads

A worker credential reads its own intake and the sessions bound to it. It may also read, through
`GET` only, a session (`show`, `events`) or intake (`show`, `history`) when all of these hold:

- The worker's own intake was created by a requester, not by a worker session. A worker therefore
  cannot mint grants for its child intakes.
- That intake's original title or text names the target by full id or 8-hex short id.
- The target is in the worker's own project.

The edge resolves the target with a routing-only service identity, and the worker's project object
decides. The request is then forwarded with an actor whose only scope is reading that one object
(`session:read` or `intake:read`, `resource_id` = target). Commands, send, capture, wakes, append,
edit and start keep the normal denial. The first read of each target appends one `evidence_read`
event to the reader's intake (audit). Nothing is written to the target.

Anything else returns `403 evidence_not_referenced` with a `reason` (`not_referenced`,
`intake_authored_by_worker`, `own_intake_not_in_project`) and an `operator_command`, for example
`hub sessions show <full id>`. The worker records that as a `permission` blocker for an operator to
run. Cross-project targets keep Control's original `missing scope intake:read` refusal.
