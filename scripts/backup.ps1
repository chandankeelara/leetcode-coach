# Copy coach.db to <BackupDir>\coach-YYYYMMDD-HHMMSS.db, keep the newest 30.
# Usage: .\scripts\backup.ps1 [-Db path] [-BackupDir path]
param(
    [string]$Db = $(if ($env:COACH_DB) { $env:COACH_DB } else { Join-Path (Split-Path $PSScriptRoot) "coach.db" }),
    [string]$BackupDir = (Join-Path (Split-Path $PSScriptRoot) "backups")
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $BackupDir | Out-Null
$out = Join-Path $BackupDir ("coach-{0}.db" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
Copy-Item $Db $out
Write-Host "wrote $out"
Get-ChildItem $BackupDir -Filter "coach-*.db" | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 | Remove-Item
