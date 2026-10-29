# Install a Hub session host

This installer is under review. We still need to verify the published entrypoints and enrollment on a fresh Windows profile.

Run one command in your ordinary user account. You don't need an administrator terminal.

On Windows, including ARM64, use PowerShell:

```powershell
irm https://hub.dtcdev.click/cli/host-install.ps1 | iex
```

On Windows, you'll find the installation in `%LOCALAPPDATA%\Programs\agent-hub`. Your logon task runs the controller with limited privileges.

On Linux or macOS, including ARM64:

```bash
curl -fsSL https://hub.dtcdev.click/cli/host-install.sh | sh
```

On Linux and macOS, you'll find the installation in `~/.local/share/agent-hub`. On Linux, we run the controller through your systemd user service. On macOS, we use your LaunchAgent.

Approve the displayed Hub device code in your browser. That approval enrolls the machine and requests access to the repositories your Hub operator has approved. You don't need a separate GitHub login on the host.

We install Python, Node and Git in your account, then add aplexer to manage sessions. We include `quse` for quota reporting.

Your engine commands include Claude Code, Codex and OpenCode. We also install Gemini CLI, Grok and Antigravity. On Windows we supply Git Bash so Claude can run without another install.

We select Claude with Opus as your default engine. Your host then receives the engine accounts configured centrally in Hub. Account delivery and a successful model session are separate from installing an executable.

We support installation paths for Linux with glibc 2.28 or newer, macOS 11 or newer, and Windows 10/11 on x64 and ARM64. macOS uses the matching aplexer binary and client from release `0.1.22`. Linux and Windows use `0.1.23`. ZCode/ZAI installation across these platforms remains unfinished.

Keep your user session active so its controller can report availability. Open a new terminal to pick up the saved engine commands on PATH. Windows PowerShell also receives the updated PATH when installation finishes.

The final JSON reports your machine ID, controller type and repository-access configuration. If `repository_access` says `not_configured`, your Hub operator must enable repository access before private repositories can be cloned. Installation doesn't create accounts, keys or paid resources.

If a download or installation step fails, rerun the same command. We preserve existing host credentials and repository work. Look in `controller.log` inside the installation directory if the controller doesn't start. The installer refuses launchers in temporary storage or changes to another user profile. Use a disposable container for installation tests.
