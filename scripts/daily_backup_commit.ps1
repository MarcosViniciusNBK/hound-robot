param(
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [string]$Branch = "main",
    [string]$ApiUrl = "http://127.0.0.1:8000/api/admin/checkpoint"
)

$ErrorActionPreference = "Stop"
Set-Location $RepoPath

# Flush SQLite WAL before reading/committing the database file
try {
    Invoke-RestMethod -Uri $ApiUrl -Method Post -TimeoutSec 5 | Out-Null
} catch {
    Write-Host "Aviso: API offline ou indisponivel para checkpoint ($($_.Exception.Message))."
}

git add data/hound_robot.db data/models/latest_model.joblib 2>$null

$changes = git status --porcelain -- data/hound_robot.db data/models/latest_model.joblib
if (-not $changes) {
    Write-Host "Sem alteracoes de estudo para commitar hoje."
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "chore(data): estudo diario automatico ($stamp)"
git push origin $Branch
Write-Host "Snapshot diario commitado e enviado para origin/$Branch em $stamp."
