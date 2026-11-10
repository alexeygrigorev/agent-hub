# Hub2 session admission decision

**Status:** Approved by Alexey in t2895u and t2896u, intake
`9606a525-dd7a-478e-b6dd-39780c863671` (2026-11-10).

## Decision

- Remove Hub2 per-machine/resource placement gates and account credential or
  quota pre-start refusals. Accept starts and pick an allowed model by quota
  headroom; retry another allowed route after an authentication failure.
- Enforce one global limit of 50 sessions using fresh host-reported native
  process and pane activity. DB session state alone does not count as active.
- Reap dead, exited, and six-hour-idle sessions every three minutes. Confirmed
  stops release the live count immediately. Provide `hub sessions reap --dry-run`.
- Keep the desktop Resource Gate, its VM, Caddy/TLS, and its systemd service
  outside this change.

## Consequences

Hub no longer predicts host resource availability before accepting work. A host
may still fail to launch a chosen engine; Hub records that result and advances
to another allowed model. The native supervisor remains responsible for
process lifecycle and stop confirmation.
