param(
  [Parameter(Mandatory=$true)][string]$Runner,
  [string]$TaskName,
  # Test seam: print the task name this invocation would install and exit.
  [switch]$ResolveTaskNameOnly
)
$ErrorActionPreference = 'Stop'
$runnerPath = (Resolve-Path -LiteralPath $Runner).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$user = $identity.Name
# Task Scheduler stores local accounts without the machine prefix
# (Tabo\alexe registers as alexe), so ownership comparisons must accept
# both forms plus the SID (intake c964dbd6 follow-up).
$account = $user.Split('\')[-1]
$ownedByMe = {
  param($PrincipalUserId)
  $PrincipalUserId -and ($user, $account, $identity.User.Value) -ccontains $PrincipalUserId
}
# Bounded failure taxonomy (intake c964dbd6 AC1): the supervisor reports the
# marker printed here as autostart_error next to autostart_state. Only these
# tokens are ever emitted; anything unrecognized classifies as script_error.
# No paths, account names or command lines are printed — the token alone
# carries no secrets.
$script:autostartErrorEmitted = $false
function Write-AutostartError {
  param([string]$Token)
  $script:autostartErrorEmitted = $true
  Write-Output "AUTOSTART_ERROR=$Token"
}
function Get-AutostartErrorToken {
  param($Err)
  $text = "$Err $($Err.Exception.Message) $($Err.CategoryInfo) $($Err.FullyQualifiedErrorId)"
  if ($text -match '(?i)another user|belongs to') { return 'task_exists_other_user' }
  if ($text -match '(?i)logon type|0x80070569') { return 'logon_type_denied' }
  if ($text -match '(?i)access (is )?denied|0x80070005|unauthorized|privilege') { return 'access_denied' }
  return 'script_error'
}
function Resolve-TaskName {
  if (-not [string]::IsNullOrWhiteSpace($TaskName)) { return $TaskName }
  # A legacy boot task may have an administrator-owned ACL; create this
  # user's task instead of modifying or stopping that existing task. When
  # the legacy task is already this user's, upgrade it in place so the
  # machine keeps ONE supervisor loop (two loops would fight over the host
  # journal lock).
  $legacy = Get-ScheduledTask -TaskName 'AgentHubSessionHost' -ErrorAction SilentlyContinue
  if ($legacy -and (& $ownedByMe $legacy.Principal.UserId)) {
    return 'AgentHubSessionHost'
  }
  return "AgentHubSessionHost-User-$($identity.User.Value)"
}
if ($ResolveTaskNameOnly) {
  Write-Output (Resolve-TaskName)
  exit 0
}
$TaskName = Resolve-TaskName
if ($TaskName.Length -gt 238 -or $TaskName -notmatch '^AgentHubSessionHost(?:-[A-Za-z0-9_-]+)?$') {
  throw 'Invalid Hub task name'
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not (& $ownedByMe $existing.Principal.UserId)) {
  Write-AutostartError 'task_exists_other_user'
  throw 'Existing task belongs to another user'
}
$psExe = (Get-Command powershell.exe).Source
$psArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`""
# -WindowStyle Hidden still flashes a console for an Interactive-logon task
# (Alexey, intake 446227dc: terminal pop-ups are distracting). conhost
# --headless starts the console host with no window at all.
$conhost = Join-Path $env:SystemRoot 'System32\conhost.exe'
if (Test-Path -LiteralPath $conhost) {
  $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$psExe`" $psArgs"
} else {
  $action = New-ScheduledTaskAction -Execute $psExe -Argument $psArgs
}
# BootTrigger registration requires admin. A durable repeating time trigger
# catches reboot/wake with StartWhenAvailable and does not require a logon.
# No repetition duration means indefinite repetition (Task Scheduler v2).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew -WakeToRun
# S4U survives a logout, but registering an S4U principal is denied to a
# non-elevated user on locked-down hosts (intake c964dbd6, Tabo). The
# Interactive fallback registers the SAME per-user Limited task with the same
# timer; it runs in this user's logon session, which is what the working
# legacy Tabo task already uses. A policy denying both is an explicit
# installation failure (the second error propagates).
$register = {
  param($LogonType)
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $LogonType -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null
}
try {
  & $register 'S4U'
} catch {
  try {
    & $register 'Interactive'
  } catch {
    Write-AutostartError (Get-AutostartErrorToken $_)
    throw
  }
}
try {
  Start-ScheduledTask -TaskName $TaskName
} catch {
  if (-not $script:autostartErrorEmitted) {
    Write-AutostartError (Get-AutostartErrorToken $_)
  }
  throw
}
$taskRecord = Join-Path (Split-Path -Parent $runnerPath) 'autostart-task-name.txt'
Set-Content -LiteralPath $taskRecord -Value $TaskName -Encoding ASCII
Write-Output 'Host autostart registered: per-user Limited, one-minute timer, no elevation. Reboot/wake verification still required.'
