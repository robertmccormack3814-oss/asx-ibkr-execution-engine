$ErrorActionPreference = 'Stop'

$TaskName = 'ASX IBKR Execution Engine'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runner = Join-Path $Root 'run_scheduled.py'
$Py = (Get-Command py.exe).Source

if (-not (Test-Path $Runner)) {
    throw "Scheduled runner not found: $Runner"
}

$Action = New-ScheduledTaskAction -Execute $Py -Argument ('"' + $Runner + '"') -WorkingDirectory $Root
$Start = (Get-Date).AddMinutes(1)
$Trigger = New-ScheduledTaskTrigger -Once -At $Start -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description 'Runs the ASX IBKR execution engine every 15 minutes; run_scheduled.py limits actual engine runs to ASX weekdays 09:55-16:15 Sydney time.' -Force | Out-Null

Write-Host "Created scheduled task: $TaskName"
Write-Host "Trigger: every 15 minutes"
Write-Host "Engine window: weekdays 09:55-16:15 Australia/Sydney"
Write-Host "Runner: $Runner"
