param(
  [Parameter(Mandatory=$true)][string]$Runner,
  [string]$TaskName = 'AgentHubSessionHost'
)
$ErrorActionPreference = 'Stop'
$runnerPath = (Resolve-Path -LiteralPath $Runner).Path
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.Principal.UserId -ne $user -and $existing.Principal.UserId -ne [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
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
Write-Output 'Host autostart registered: per-user S4U, one-minute timer, no logon dependency. Reboot/wake verification still required.'
