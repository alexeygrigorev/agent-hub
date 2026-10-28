# One-time (bootstrap) update of a Windows Agent Hub session host such as Tabo.
#
# Run in PowerShell on the host, as the Windows user that owns the
# AgentHubSessionHost scheduled task:
#
#   git -C C:\repos\agent-hub pull --ff-only
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\repos\agent-hub\scripts\windows-host-update.ps1
#
# It fast-forwards the clean main checkout, installs dependencies (quse) into
# the existing service venv, smoke-tests the new code, then restarts the task
# and its own hub-controller sessions. It never discards, stashes or resets
# local work and prints no credentials. After this, later releases arrive
# without anyone at the machine: an operator pins a commit with
# `hub host-release --machine <id> --sha <sha>` (intake a3227276).
param([string]$Repo = 'C:\repos\agent-hub', [string]$TaskName = 'AgentHubSessionHost')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $Repo
$branch = (& git branch --show-current)
if ($LASTEXITCODE -ne 0 -or $branch.Trim() -ne 'main') { throw 'Expected the main checkout; nothing was changed.' }
$dirty = @(& git status --porcelain=v1 --untracked-files=no)
if ($LASTEXITCODE -ne 0 -or $dirty.Count -ne 0) { throw 'Tracked local changes are present; reconcile them first. Nothing was discarded.' }
& git fetch --quiet origin main
if ($LASTEXITCODE -ne 0) { throw 'git fetch failed.' }
& git merge --ff-only --quiet origin/main
if ($LASTEXITCODE -ne 0) { throw 'Fast-forward failed; no reset was attempted.' }
$python = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Service venv interpreter missing: $python" }
& $python -m pip install --quiet --disable-pip-version-check -e $Repo
if ($LASTEXITCODE -ne 0) { throw 'pip install failed; the service was not restarted.' }
$env:PYTHONPATH = Join-Path $Repo 'host'
& $python -c "import importlib.util; from agent_hub_host import system, self_update, session_host, fleet; assert importlib.util.find_spec('quse.cli'); print('arch=' + system.get_arch()); print('checkout=' + str(self_update.repo_root()))"
if ($LASTEXITCODE -ne 0) { throw 'Smoke test failed; the service was not restarted.' }

Stop-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
# Stopping the task can leave the supervisor's native controller running on the
# old code, holding the journal lock. Stop only hub-controller-* sessions in
# this checkout; agent sessions are untouched.
$aplexer = Join-Path $env:LOCALAPPDATA 'Programs\aplexer\a.exe'
if (-not (Test-Path -LiteralPath $aplexer)) { $aplexer = 'a' }
$listing = (& $aplexer list --json) | ConvertFrom-Json
$rows = if ($listing.sessions) { $listing.sessions } else { $listing }
foreach ($row in @($rows)) {
  if ($row.tag -like 'hub-controller-*' -and $row.workspace -eq $Repo -and ($row.phase -eq 'running' -or $row.state -eq 'running')) {
    & $aplexer kill $row.id --signal TERM --grace-ms 30000 | Out-Null
    Write-Output ('stopped_controller=' + $row.tag)
  }
}
Start-ScheduledTask -TaskName $TaskName
Write-Output ('host_source=' + (& git rev-parse HEAD).Trim())
Write-Output 'Restarted. Verify from Hetzner: hub routing-machines (arch arm64, no codex_quota_gate rejections, host_release state).'
