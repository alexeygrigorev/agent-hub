# Running session control

Use the portable CLI without installing packages:

```sh
export HUB_CONFIG_FILE=/absolute/path/to/private-agent-config.json
scripts/hub sessions start --machine MACHINE_ID --workspace WORKSPACE_LABEL --engine opencode --interactive --key UNIQUE_KEY --wait
scripts/hub sessions send SESSION_ID --body-file message.txt --key UNIQUE_SEND_KEY --wait
scripts/hub sessions capture SESSION_ID --max-bytes 4096 --key UNIQUE_CAPTURE_KEY --wait
scripts/hub sessions stop SESSION_ID --key UNIQUE_STOP_KEY --wait
```

Pair the caller with `hub login URL --kind agent --name NAME --scope fleet:launch --scope bus:send --scope bus:read` and approve the device in the Hub. Keep caller and host credentials separate. Configuration files and their parent directories must be private (0600 and 0700 on POSIX).

The host runs the existing Fleet dispatcher and `sessions host-receive --once` in a genuinely bound native controller session. Fleet publishes the native catalog, discovers pending starts through `/api/sessions/host-bindings`, and reports exact native bindings. A temporary 404 during deployment rollout delays binding reconciliation without preventing Fleet dispatch.

`--wait` on send requires a correlated reply from the intended native session; a persisted inbox message alone does not pass. This proves receipt by a cooperative receiver, not completion of the requested work. Stop waits for the stopped state and releases Fleet capacity after confirmed workload exit.

For a dedicated acceptance workspace, a trusted local subscription may set `startup_argv` to start the configured engine with a receiver bootstrap prompt. `scripts/session-smoke-receiver.py` is a bounded cooperative receipt helper that the real target agent can run. It prints received test messages and echoes their exact digest and correlation. It does not execute tasks. Never use it to claim an arbitrary agent processed or completed work.

Run the complete public CLI acceptance loop against that configured workspace:

```sh
python3 scripts/session-smoke.py --machine MACHINE_ID --workspace WORKSPACE_LABEL --engine opencode --timeout 180
```

The test creates its own throwaway, checks automatic binding, confirmed receipt and capture, and stops only that session. It attempts cleanup on failure and reports the owned session id if reconciliation is needed. It requires no SSH or native session control on the caller machine.

On 2026-10-17 the deployed Hub passed this loop against a real Muse Spark 1.3 session on Hetzner, including capture and confirmed containment exit. Physical execution from the Grok Bot box and unattended host-service recovery remain separate deployment tasks.
