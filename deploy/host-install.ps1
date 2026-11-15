# Hub2 Windows host installer and updater (intake 41ef7048).
#
#   irm https://hub.dtcdev.click/cli/host-install.ps1 | iex
#   hub2 self-update            (runs this script, verified against host-release.json)
#
# Installs the verified published host wheel into %LOCALAPPDATA%\agent-hub2\venv,
# keeps `hub2 serve` running under the per-user scheduled task AgentHub2Host
# (log: %LOCALAPPDATA%\agent-hub2\serve.log), registers the daily
# AgentHub2HostUpdate task (`hub2 self-update`) and retires the legacy
# AgentHubController/AgentHubSessionHost supervisor so it stops cycling.
#
# No param() block, so it also runs under `irm | iex`. Optional environment:
#   HUB_BASE_URL               Hub origin (default https://hub.dtcdev.click)
#   HUB2_APLEXER               native aplexer binary; default: previous install, then PATH
#   HUB2_CREDENTIAL_MODE       local (host-owned logins) or central (Hub-delivered)
#   HUB2_PYTHON                base Python >= 3.11 used to create the venv
#   HUB2_RETIRE_LEGACY=0       keep the legacy supervisor tasks untouched
#   HUB2_PAIRING_CODE          first enrollment only (with HUB2_WORKSPACE_ROOT)
#
# It only ever stops processes whose command line is this Hub2 venv's `serve`,
# the legacy session-host supervisor/controller loop, or aplexer *shell*
# sessions tagged hub-controller-*. Codex/Claude/other worker processes and
# their aplexer sessions are never touched. Nothing is deleted.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Base = if ($env:HUB_BASE_URL) { $env:HUB_BASE_URL.TrimEnd('/') } else { 'https://hub.dtcdev.click' }
if ($Base -notmatch '^https://') { throw 'HUB_BASE_URL must use https' }
$Root = Join-Path $env:LOCALAPPDATA 'agent-hub2'
$Venv = Join-Path $Root 'venv'
$Hub2 = Join-Path $Venv 'Scripts\hub2.exe'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$ServeLog = Join-Path $Root 'serve.log'
$ServeErr = Join-Path $Root 'serve.stderr.log'
$InstallLog = Join-Path $Root 'install.log'
$Settings = Join-Path $Root 'host-task.json'
$Runner = Join-Path $Root 'run-host.ps1'
$SavedInstaller = Join-Path $Root 'host-install.ps1'
$TaskName = 'AgentHub2Host'
$UpdateTaskName = 'AgentHub2HostUpdate'
$LegacyControl = Join-Path $env:LOCALAPPDATA 'Programs\agent-hub\controller'
New-Item -ItemType Directory -Force -Path $Root, (Join-Path $Root 'tmp') | Out-Null

function Write-Step([string]$Message) {
  $line = (Get-Date).ToString('o') + ' ' + $Message
  Write-Host $line
  Add-Content -LiteralPath $InstallLog -Value $line
}

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$Log) {
  # Windows PowerShell 5.1 turns native stderr under redirection into a
  # terminating error when EAP is Stop; run natives with Continue and check
  # the exit code explicitly.
  $previous = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    if ($Log) { & $Exe @Arguments *>> $Log } else { & $Exe @Arguments 2>&1 | Out-Null }
    return $LASTEXITCODE
  } finally { $ErrorActionPreference = $previous }
}

function Get-Verified([string]$Name, $Record, [string]$Directory) {
  $target = Join-Path $Directory $Name
  Invoke-WebRequest -UseBasicParsing -Uri ($Base + '/cli/' + $Name) -OutFile $target
  $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
  if ($hash -ne $Record.sha256 -or (Get-Item -LiteralPath $target).Length -ne $Record.size) {
    throw "checksum mismatch for $Name"
  }
  return $target
}

function Find-BasePython {
  $candidates = @()
  if ($env:HUB2_PYTHON) { $candidates += , @($env:HUB2_PYTHON) }
  if (Get-Command py.exe -ErrorAction SilentlyContinue) { $candidates += , @('py.exe', '-3.13'); $candidates += , @('py.exe', '-3.12'); $candidates += , @('py.exe', '-3.11') }
  foreach ($name in 'python.exe', 'python3.exe') {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found -and $found.Source -notlike '*WindowsApps*') { $candidates += , @($found.Source) }
  }
  foreach ($candidate in $candidates) {
    $exe = $candidate[0]; $extra = @($candidate | Select-Object -Skip 1)
    try {
      $ok = & $exe @extra -c 'import sys; print(int(sys.version_info >= (3, 11)))' 2>$null
      if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq '1') { return , $candidate }
    } catch { }
  }
  throw 'Python 3.11+ not found; install it or set HUB2_PYTHON'
}

function Get-ServeProcesses {
  # This venv's serve daemon only (the venv launcher and its interpreter child).
  Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine.IndexOf($Venv, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
    $_.CommandLine -match '(^|\s)serve(\s|$)' -and $_.Name -match '^(hub2|python|pythonw)\.exe$'
  }
}

function Stop-Hub2Host {
  # `hub2 self-update` starts this script and exits; pip cannot replace a
  # running hub2.exe, so wait for that launcher first.
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline -and @(Get-CimInstance Win32_Process | Where-Object {
      $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.IndexOf($Venv, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
      $_.CommandLine -match '(^|\s)self-update(\s|$)' }).Count -gt 0) { Start-Sleep -Milliseconds 500 }
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -and $task.State -eq 'Running') {
    Write-Step "stopping scheduled task $TaskName"
    Stop-ScheduledTask -TaskName $TaskName
  }
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-ServeProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
  foreach ($process in @(Get-ServeProcesses)) {
    Write-Step "stopping hub2 serve pid=$($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
}

function Resolve-Aplexer {
  if ($env:HUB2_APLEXER) { return (Resolve-Path -LiteralPath $env:HUB2_APLEXER).Path }
  if (Test-Path -LiteralPath $Settings) {
    $saved = (Get-Content -Raw -LiteralPath $Settings | ConvertFrom-Json).aplexer
    if ($saved -and (Test-Path -LiteralPath $saved)) { return $saved }
  }
  foreach ($name in 'aplexer.exe', 'a.exe') {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  throw 'aplexer not found; set HUB2_APLEXER to the native aplexer binary'
}

function Retire-LegacyHost {
  if ($env:HUB2_RETIRE_LEGACY -eq '0') { Write-Step 'legacy retirement skipped (HUB2_RETIRE_LEGACY=0)'; return }
  # Cooperative stop first: the legacy runner/supervisor exit 76 on this marker.
  if (Test-Path -LiteralPath (Split-Path -Parent $LegacyControl)) {
    New-Item -ItemType Directory -Force -Path $LegacyControl | Out-Null
    Set-Content -LiteralPath (Join-Path $LegacyControl 'replace-pending') -Value ('{"requested_at":' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + '}') -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $LegacyControl 'retired-by-hub2') -Value ((Get-Date).ToString('o') + ' retired by Hub2 host-install.ps1') -Encoding ASCII
  }
  $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like 'AgentHubController-*' -or $_.TaskName -like 'AgentHubSessionHost*' })
  foreach ($task in $tasks) {
    try {
      if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath }
      if ($task.State -ne 'Disabled') { Disable-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath | Out-Null }
      Write-Step "legacy task disabled: $($task.TaskName)"
    } catch { Write-Step "legacy task left unchanged (not owned): $($task.TaskName): $($_.Exception.Message)" }
  }
  $legacy = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and ($_.CommandLine -match 'session-host-supervisor\.py' -or
      ($_.Name -eq 'powershell.exe' -and $_.CommandLine -match 'agent-hub\\(releases\\[^\\]+\\)?controller\.ps1'))
  })
  foreach ($process in $legacy) {
    Write-Step "stopping legacy supervisor pid=$($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
  # The supervisor's controller runs as an aplexer shell session tagged
  # hub-controller-*. Stop only those; never codex/claude/other sessions.
  try {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $listing = & $script:Aplexer list --json --running 2>$null } finally { $ErrorActionPreference = $previous }
    foreach ($session in @($listing | ConvertFrom-Json)) {
      $tag = if ($session.PSObject.Properties['tag']) { [string]$session.tag } else { '' }
      $engine = if ($session.PSObject.Properties['engine']) { [string]$session.engine } else { '' }
      if ($tag -like 'hub-controller-*' -and $engine -eq 'shell') {
        Write-Step "stopping legacy controller session $($session.id) tag=$tag"
        Invoke-Native $script:Aplexer @('kill', [string]$session.id) $InstallLog | Out-Null
      }
    }
  } catch { Write-Step "legacy controller session scan failed: $($_.Exception.Message)" }
}

Write-Step "Hub2 host install from $Base (mode=$(if ($env:HUB2_INSTALL_MODE) { $env:HUB2_INSTALL_MODE } else { 'install' }))"
$manifest = Invoke-RestMethod -UseBasicParsing -Uri ($Base + '/cli/host-release.json')
$version = [string]$manifest.host_version
if ($version -notmatch '^[0-9a-f]{40}$') { throw 'invalid published host_version' }
if ($env:HUB2_EXPECTED_HOST_VERSION -and $env:HUB2_EXPECTED_HOST_VERSION -ne $version) { throw 'published release changed during update; retry' }
$artifacts = $manifest.artifacts
$wheelName = @($artifacts.PSObject.Properties.Name | Where-Object { $_ -match '^agent_hub_host_v2-2\.0\.0\+g[0-9a-f]{40}-py3-none-any\.whl$' })
if ($wheelName.Count -ne 1 -or $wheelName[0] -notlike "*$version*") { throw 'release wheel missing' }
$download = Join-Path $Root ('tmp\' + $version)
New-Item -ItemType Directory -Force -Path $download | Out-Null
$wheel = Get-Verified $wheelName[0] $artifacts.($wheelName[0]) $download
Get-Verified 'websockets-16.0-py3-none-any.whl' $artifacts.'websockets-16.0-py3-none-any.whl' $download | Out-Null
$installer = Get-Verified 'host-install.ps1' $artifacts.'host-install.ps1' $download
Write-Step "verified release $version"

$script:Aplexer = Resolve-Aplexer
Write-Step "aplexer: $script:Aplexer"
Stop-Hub2Host
if (-not (Test-Path -LiteralPath $VenvPython)) {
  $python = Find-BasePython
  Write-Step "creating venv with $($python -join ' ')"
  $pythonExe = $python[0]; $pythonArgs = @($python | Select-Object -Skip 1)
  if ((Invoke-Native $pythonExe (@($pythonArgs) + @('-m', 'venv', $Venv)) $InstallLog) -ne 0) { throw 'venv creation failed' }
}
$code = Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-index', '--find-links', $download, '--force-reinstall', $wheel) $InstallLog
if ($code -ne 0) { throw "pip install failed; see $InstallLog" }
$installed = (& $Hub2 version | ConvertFrom-Json)
if ($installed.host_version -ne $version) { throw "installed host_version $($installed.host_version) != $version" }
Copy-Item -LiteralPath $installer -Destination $SavedInstaller -Force
Write-Step "installed host_version=$version"

$config = Join-Path $env:USERPROFILE '.config\agent-hub\v2.json'
if (-not (Test-Path -LiteralPath $config)) {
  if (-not $env:HUB2_PAIRING_CODE) { throw "not enrolled: set HUB2_PAIRING_CODE (and HUB2_WORKSPACE_ROOT) and rerun" }
  $workspaceRoot = if ($env:HUB2_WORKSPACE_ROOT) { $env:HUB2_WORKSPACE_ROOT } else { $env:USERPROFILE }
  & $Hub2 --server $Base enroll --root $workspaceRoot --engine codex --engine claude --engine go
  if ($LASTEXITCODE -ne 0) { throw 'enrollment failed' }
}
if ($env:HUB2_CREDENTIAL_MODE) {
  & $Hub2 credential-mode $env:HUB2_CREDENTIAL_MODE | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'credential mode change failed' }
}
Write-Step ("credential_mode=" + (& $Hub2 credential-mode | ConvertFrom-Json).credential_mode)

@{ aplexer = $script:Aplexer; host_version = $version } | ConvertTo-Json | Set-Content -LiteralPath $Settings -Encoding UTF8
$q = { param($s) "'" + ($s -replace "'", "''") + "'" }
@(
  '# Generated by host-install.ps1: keep hub2 serve running; never exits on its own.'
  '$ErrorActionPreference = ''Continue'''
  '$hub2 = ' + (& $q $Hub2)
  '$aplexer = ' + (& $q $script:Aplexer)
  '$log = ' + (& $q $ServeLog)
  '$err = ' + (& $q $ServeErr)
  'while ($true) {'
  '  Add-Content -LiteralPath $log -Value ((Get-Date).ToString(''o'') + '' runner: starting hub2 serve'')'
  '  & cmd.exe /d /c ("`"" + $hub2 + "`" serve --aplexer `"" + $aplexer + "`" --log-file `"" + $log + "`" >> `"" + $err + "`" 2>&1")'
  '  $code = $LASTEXITCODE'
  '  Add-Content -LiteralPath $log -Value ((Get-Date).ToString(''o'') + '' ERROR runner: hub2 serve exited code='' + $code + ''; heartbeat and restart in 30 s'')'
  '  # The HTTP heartbeat is Hub''s "running but disconnected" evidence (AC2 alert).'
  '  & cmd.exe /d /c ("`"" + $hub2 + "`" heartbeat --aplexer `"" + $aplexer + "`" >> `"" + $err + "`" 2>&1")'
  '  Start-Sleep -Seconds 30'
  '}'
) | Set-Content -LiteralPath $Runner -Encoding UTF8

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $Runner + '"')
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
  -Settings $settings -Principal $principal -Description 'Hub2 host daemon (hub2 serve); log: serve.log' -Force | Out-Null
$updateAction = New-ScheduledTaskAction -Execute $Hub2 -Argument 'self-update'
$updateSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $UpdateTaskName -Action $updateAction -Trigger (New-ScheduledTaskTrigger -Daily -At '04:17') `
  -Settings $updateSettings -Principal $principal -Description 'Hub2 host verified self-update' -Force | Out-Null
Write-Step "registered $TaskName and $UpdateTaskName"

Retire-LegacyHost
Start-ScheduledTask -TaskName $TaskName
$deadline = (Get-Date).AddSeconds(90); $connected = $false
while ((Get-Date) -lt $deadline -and -not $connected) {
  Start-Sleep -Seconds 3
  try {
    $self = & $Hub2 request GET /api/fleet/routing/self 2>$null | ConvertFrom-Json
    $connected = ($self.connected -eq $true -and $self.host_version -eq $version)
  } catch { }
}
if (-not $connected) { throw "AgentHub2Host did not connect with host_version $version within 90 s; see $ServeLog and $ServeErr" }
Write-Step "Hub2 host connected machine=$($self.machine_id) host_version=$($self.host_version)"
