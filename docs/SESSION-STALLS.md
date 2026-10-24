# Stalled sessions

Hub marks a running session `stalled` after 30 minutes without native
progress. This applies to headless and interactive sessions, including idle
prompts. Set the existing Worker variable `HUB_AGENT_STALL_S` to change the
threshold (positive seconds, at most seven days). The existing liveness
check evaluates sessions every five minutes and sends one operator alert
through the configured Hub alert inbox/webhook for each stall episode.

Progress is the newest terminal-output or genuine state-report event, a
workload CPU increase of at least 100ms, or a change in bounded workspace
file metadata. Catalog polls, worker housekeeping and repeated timestamps
do not count. No file contents, argv or process details leave the host.
The filesystem scan skips dependencies, caches and generated build trees,
and has a 2,048-file/50ms bound; CPU scans have a 256-process/50ms bound.
An unavailable sample remains unknown. These limits make the observation
conservative for very large workspaces.

CLI list/show, web session views and MCP session list/show expose `state:
stalled`, `stall_reason`, `stalled_since`, `stall_threshold` and the original
`lifecycle_state`. A stall retains running lifecycle custody, capacity and
send/capture/stop controls. A minute of fresh progress clears it; a stale
or missing host observation does not prove recovery. Alert delivery and
retry deduplication use the existing liveness alert channel.

Headless agent death is separate from child-tree liveness. A known absent
or zombie workload leader with live containment reports
`agent_exited_children_running` immediately, even if children still emit
output or consume CPU. The host reaps children only for an exact owned
headless journal binding whose leader this monitor previously saw alive.
It uses the existing native stop with a one-second TERM grace and bounded
native calls, at most one cleanup per poll. Uncertain cleanup is reported
and never blindly retried. Sessions discovered already orphaned are
reported for operator action. Interactive sessions are never auto-stopped.
Only the existing positive exit oracle can release capacity or mark a
session stopped; phase labels, idle time and deleted paths cannot do so.

The three-hour warning from intake `7fc3ca24` remains a duration warning
with its existing CLI ack/snooze. It now uses Hub's running lifecycle even
when a native phase disagrees, and uses the newest actual progress for idle
time. Its snooze never hides stall alerts. Stalls use the single existing
liveness evaluator and alert channel, with no additional scheduler.
