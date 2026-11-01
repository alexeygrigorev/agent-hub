param(
  [Parameter(Mandatory=$true)][string]$Runner,
  [string]$TaskName
)
$ErrorActionPreference = 'Stop'
$runnerPath = (Resolve-Path -LiteralPath $Runner).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$user = $identity.Name
if ([string]::IsNullOrWhiteSpace($TaskName)) {
  # A legacy boot task may have an administrator-owned ACL; create this user's
  # task instead of modifying or stopping that existing task. When the legacy
  # task is already this user's, upgrade it in place so the machine keeps ONE
  # supervisor loop (two loops would fight over the host journal lock).
  $legacy = Get-ScheduledTask -TaskName 'AgentHubSessionHost' -ErrorAction SilentlyContinue
  if ($legacy -and ($legacy.Principal.UserId -eq $user -or $legacy.Principal.UserId -eq $identity.User.Value)) {
    $TaskName = 'AgentHubSessionHost'
  } else {
    $TaskName = "AgentHubSessionHost-User-$($identity.User.Value)"
  }
}
if ($TaskName.Length -gt 238 -or $TaskName -notmatch '^AgentHubSessionHost(?:-[A-Za-z0-9_-]+)?$') {
  throw 'Invalid Hub task name'
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Principal.UserId -ne $user -and $existing.Principal.UserId -ne $identity.User.Value) {
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
