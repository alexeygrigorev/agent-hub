# Launcher releases (intake 4bf458de)

`hub routing-machines` includes `launcher_release` beside `host_release`:
`root`, `current_sha`, `desired_sha`, `state`, `error`, and reporting/pin timestamps.
The root comes from the host's launcher configuration, never from the pin.
Prepared immutable releases named by a full SHA report that SHA; they cannot be
updated in place. A host without a launcher reports `unsupported`.

After deploying the Hub API and installing this host version, an authenticated
operator can set or clear a per-machine pin:

```sh
hub launcher-release --machine MACHINE_ID --sha FULL_40_HEX_SHA
hub launcher-release --machine MACHINE_ID --clear
hub routing-machines
```

Only an administrator can set pins. Hosts can read their own pin and report
their own state. Clearing a pin does not downgrade the launcher. The existing
outbound host-release poll (120 seconds) also polls launcher pins; no SSH or
inbound Windows access is needed. The configured Python interpreter (including
`python.exe`) runs the smoke import. Launcher commands use the updated checkout
on their next invocation; the Hub controller need not restart.

Updates require an exact clean `main` checkout, a target reachable from its own
`origin/main`, and a fast-forward from current HEAD. Untracked files also cause
refusal. Git subprocess output stays private and is never logged or uploaded.
A process lock uses `msvcrt` on Windows and `flock` on Unix. A failed merge or
smoke import, including a subprocess timeout, attempts `git reset --keep` to the
previous commit. No forced reset, clean, credential changes, pip install, or
shell command is used. The smoke uses a disposable bytecode cache inside the
checkout's Git directory and removes only that cache. Rollback failure is
reported explicitly; it must never be called success.

## Acceptance evidence still required

Local tests cover real Git transitions and failure guards, mocked Windows Python
invocation and lock behavior, and API authorization/isolation. These tests do
not prove an installed Tabo host was updated. After authorized deployment and
host bootstrap, record Tabo's previous SHA, pin
`343648aa8c2230f802cc4da2c6a12148144e26b4` or a later launcher main commit,
then capture fresh `launcher_release` reporting the desired/current match and
healthy host operation. This session does not authorize deployment. Keep the
intake open until that live evidence and independent review exist.
