# Install a Hub session host

Fresh native Windows enrollment is still being verified. The macOS runtime can't enforce Hub's required resource limits, so its controller won't start.

Run one command in your ordinary user account. You don't need an administrator terminal.

On Windows, including ARM64, use PowerShell:

```powershell
irm https://hub.dtcdev.click/host/install.ps1 | iex
```

On Windows, you'll find the installation in `%LOCALAPPDATA%\Programs\agent-hub`. Your logon task runs the controller with limited privileges.

On Linux or macOS, including ARM64:

```bash
curl -fsSL https://hub.dtcdev.click/host/install.sh | sh
```

On Linux and macOS, you'll find the installation in `~/.local/share/agent-hub`. On Linux, we run the controller through your systemd user service. On macOS, we use your LaunchAgent.

Approve the displayed Hub device code in your browser. That approval enrolls the machine and requests access to the repositories your Hub operator has approved. You don't need a separate GitHub login on the host.

We install Python, Node and Git in your account, then add aplexer to manage sessions. We include `quse` for quota reporting.

Your engine commands include Claude Code, Codex and OpenCode. We also install Gemini CLI, Grok and Antigravity. On Windows we supply Git Bash so Claude can run without another install.

We select Claude with Opus when no default exists, preserving your later choices on repairs. Your host then receives the engine accounts configured centrally in Hub. Account delivery and a successful model session are separate from installing an executable.

The installer targets Linux with glibc 2.28 or newer, macOS 11 or newer, and Windows 10/11 on x64 and ARM64. macOS uses the matching aplexer binary and client from release `0.1.22`. Linux and Windows use `0.1.23`. ZCode/ZAI installation across these platforms remains unfinished.

Keep your user session active so its controller can report availability. Open a new terminal to pick up the saved engine commands on PATH. Windows PowerShell also receives the updated PATH when installation finishes.

The final JSON reports your machine ID, controller type and repository-access configuration. `not_configured` means your Hub operator must enable repository access; `declined` or `forbidden` means access wasn't granted; `unavailable` means you can retry after a temporary outage. These repository states still allow controller installation. Installation doesn't create accounts, keys or paid resources.

If a download or installation step fails, rerun the same command. We preserve existing host credentials and repository work. Windows repairs use separate runtime releases and ask the owned controller to exit before switching the logon task. After a reboot or crash, a changed boot identity or exact native exit proof allows recovery, with the old receipt archived. Unknown state preserves the evidence and reports `controller/needs-attention.json`; restore native status access and retry. Abandoned stop requests expire. Fresh, rerun and reboot behavior still need native Windows transcripts. Look in `controller.log` inside the installation directory if the controller doesn't start. The installer refuses launchers in temporary storage or changes to another user profile. Test in a disposable profile or VM; Linux service tests need a real systemd user manager and cgroup delegation.

Earlier local test controllers without the cooperative lease must be deliberately stopped before upgrading; the installer preserves a running controller it cannot prove it owns. Those versions were never publicly shipped.
