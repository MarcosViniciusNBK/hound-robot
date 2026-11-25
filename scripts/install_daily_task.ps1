param(
    [string]$TaskName = "HoundRobotDailyBackup",
    [string]$RunAt = "23:55"
)

$ErrorActionPreference = "Stop"
$ScriptPath = Join-Path $PSScriptRoot "daily_backup_commit.ps1"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Commit e push diario automatico dos estudos do Hound Robot" `
    -Force | Out-Null

Write-Host "Tarefa agendada '$TaskName' criada para rodar diariamente as $RunAt."
