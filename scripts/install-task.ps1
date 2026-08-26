# Register the 4-hourly poll as a Windows Scheduled Task for the current user.
# No admin rights needed. Re-running replaces the existing task.
#
#   .\scripts\install-task.ps1              install, every 4 hours
#   .\scripts\install-task.ps1 -Hours 6     a different interval
#   .\scripts\install-task.ps1 -Uninstall   remove it

param(
    [int]$Hours = 4,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'JobPipelinePoll'
$root     = Split-Path -Parent $PSScriptRoot
$script   = Join-Path $PSScriptRoot 'poll.ps1'

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output "removed scheduled task '$taskName'"
    } catch {
        Write-Output "no scheduled task named '$taskName'"
    }
    exit 0
}

if (-not (Test-Path $script)) { throw "missing $script" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $root

# Repeat forever from the next quarter hour. New-grad reqs collect hundreds of
# applicants inside 48 hours, so the whole point is not waiting for a human to
# remember to run it.
$start   = (Get-Date).AddMinutes(15)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Hours $Hours)

# A laptop that only polls on mains power is a laptop that never polls.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Polls ATS sources for the job pipeline every $Hours hours." `
    -Force | Out-Null

Write-Output "registered '$taskName', every $Hours hours, first run $($start.ToString('yyyy-MM-dd HH:mm'))"
Write-Output "logs: $root\data\logs\poll-<date>.log"
Write-Output ""
Write-Output "  check:   Get-ScheduledTask -TaskName $taskName"
Write-Output "  run now: Start-ScheduledTask -TaskName $taskName"
Write-Output "  remove:  .\scripts\install-task.ps1 -Uninstall"
