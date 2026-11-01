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
  throw 'Existing task belongs to another user'
}
$psExe = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction -Execute $psExe -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`""
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
  & $register 'Interactive'
}
Start-ScheduledTask -TaskName $TaskName
$taskRecord = Join-Path (Split-Path -Parent $runnerPath) 'autostart-task-name.txt'
Set-Content -LiteralPath $taskRecord -Value $TaskName -Encoding ASCII
Write-Output 'Host autostart registered: per-user Limited, one-minute timer, no elevation. Reboot/wake verification still required.'
