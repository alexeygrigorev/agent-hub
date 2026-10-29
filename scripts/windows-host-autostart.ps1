param(
  [Parameter(Mandatory=$true)][string]$Runner,
  [string]$TaskName
)
$ErrorActionPreference = 'Stop'
$runnerPath = (Resolve-Path -LiteralPath $Runner).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$user = $identity.Name
if ([string]::IsNullOrWhiteSpace($TaskName)) {
  # A legacy boot task may have an administrator-owned ACL. Create this
  # user's task instead of modifying or stopping that existing task.
  $TaskName = "AgentHubSessionHost-User-$($identity.User.Value)"
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
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
# Never elevate, request a password, or substitute an interactive logon task.
# A policy denying S4U/batch logon is an explicit installation failure.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$taskRecord = Join-Path (Split-Path -Parent $runnerPath) 'autostart-task-name.txt'
Set-Content -LiteralPath $taskRecord -Value $TaskName -Encoding ASCII
Write-Output 'Host autostart registered: per-user S4U, one-minute timer, no logon dependency. Reboot/wake verification still required.'
