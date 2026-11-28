# Workspace discovery

`hub sessions start --repo owner/name` requests the repository basename as a
workspace label. Registration does not enroll a filesystem root on a host.
The host advertises git checkout basenames from each enrolled root and its
immediate children on every heartbeat, including linked worktrees with a
`.git` file. Newly created clones appear without restarting the daemon.
Non-git directories are not advertised. Explicit workspace aliases take
precedence, even when unavailable. Distinct checkouts with the same basename
are omitted as ambiguous. Symlinks must resolve inside an enrolled root.

The host resolves discovered labels using the same inventory at launch time.
The existing absolute-path and directory-label allowlist checks still apply.
A removed clone disappears from the next heartbeat. The heartbeat reserves
32 engine/resource hints separately from up to 2048 workspace labels, so a
large clone inventory does not suppress engine, effort or credential hints.
Heartbeat requests allow up to 256 KiB. The server rejects excess inventory rather than silently truncating it.

For existing hosts, the deployment owner must enroll every intended clone
parent in the private host configuration's `roots`, preserving existing roots,
then restart through the maintained release process. For example, a host
previously enrolled only for `work` cannot launch a clone in `git` until that
parent is enrolled. Do not add aliases outside enrolled roots or copy worktree
code into a live installation. Install the server change before adopting the
host release, since older servers accept only 32 total capability tags.

After CI deploy and host adoption, inspect the routing-machine workspace tags,
start the registered repository on that host with the normal `hub sessions
start --repo` command, verify `running`, and stop the probe with confirmed native
exit. A healthy systemd unit is not launch acceptance.
