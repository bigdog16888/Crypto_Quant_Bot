# create_version_backup.ps1 — Snapshot codebase + live DB for a version milestone.
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\create_version_backup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\create_version_backup.ps1 -IncludeEnv

param(
    [switch]$IncludeEnv
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# Read version from config/settings.py
$Version = "unknown"
$settingsPath = Join-Path $Root "config\settings.py"
if (Test-Path $settingsPath) {
    $match = Select-String -Path $settingsPath -Pattern 'VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($match) { $Version = $match.Matches[0].Groups[1].Value }
}

$Stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$BackupRoot = Join-Path $Root "backups"
$ArchiveName = "Crypto_Quant_Bot_v${Version}_${Stamp}"
$ArchiveDir = Join-Path $BackupRoot $ArchiveName
$ZipPath = "$ArchiveDir.zip"

New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null

Write-Host "=== Crypto Quant Bot Version Backup ==="
Write-Host "Version : $Version"
Write-Host "Output  : $ZipPath"
Write-Host ""

# Checkpoint WAL into main DB file for a consistent snapshot
$DbPath = Join-Path $Root "crypto_bot.db"
if (Test-Path $DbPath) {
    Write-Host "Checkpointing crypto_bot.db (WAL flush)..."
    python -c @"
import sqlite3, os
db = os.path.join(r'$Root', 'crypto_bot.db')
conn = sqlite3.connect(db)
conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
conn.close()
print('  WAL checkpoint complete.')
"@
    Copy-Item $DbPath (Join-Path $ArchiveDir "crypto_bot.db") -Force
    Write-Host "  DB copied."
} else {
    Write-Host "  WARNING: crypto_bot.db not found — skipping DB copy."
}

if ($IncludeEnv -and (Test-Path (Join-Path $Root ".env"))) {
    Copy-Item (Join-Path $Root ".env") (Join-Path $ArchiveDir ".env") -Force
    Write-Host "  .env copied (contains secrets — store archive securely)."
}

# Copy source tree, excluding volatile/runtime dirs
$ExcludeDirs = @(
    'backups', 'scratch', 'venv', 'env', 'ENV', '.git', '.pytest_cache',
    '__pycache__', '.gemini', 'node_modules', 'logs'
)
$ExcludeFiles = @('*.log', '*.pid', '*.db-shm', '*.db-wal')

Write-Host "Copying source tree..."
Get-ChildItem $Root -Force | Where-Object {
    $name = $_.Name
    if ($ExcludeDirs -contains $name) { return $false }
    if ($name -like 'pytest-cache-files-*') { return $false }
    if ($name -eq 'crypto_bot.db') { return $false }  # already copied above
    if ($name -eq '.env' -and -not $IncludeEnv) { return $false }
    return $true
} | ForEach-Object {
    $dest = Join-Path $ArchiveDir $_.Name
    if ($_.PSIsContainer) {
        Copy-Item $_.FullName $dest -Recurse -Force
    } else {
        Copy-Item $_.FullName $dest -Force
    }
}

# Strip __pycache__ from copied tree
Get-ChildItem $ArchiveDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force }

# Write manifest
$Manifest = @"
Crypto Quant Bot — Version Backup
==================================
Version   : $Version
Created   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Hostname  : $env:COMPUTERNAME
Includes  : source code, crypto_bot.db$(if ($IncludeEnv) { ', .env' } else { '' })

Restore:
  1. Stop engine (run_bot.bat → Stop Monitoring, or restart_runner.bat)
  2. Extract zip to a folder
  3. Copy crypto_bot.db over your live DB (backup current DB first!)
  4. pip install -r requirements.txt
  5. run_stack.bat

Notes:
  - scratch/ and backups/ are excluded from source copy (regenerated locally)
  - engine.log and runtime caches are excluded
  - Use -IncludeEnv only if you need API keys in the archive (keep zip private)
"@
Set-Content -Path (Join-Path $ArchiveDir "BACKUP_MANIFEST.txt") -Value $Manifest -Encoding UTF8

if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path $ArchiveDir -DestinationPath $ZipPath -CompressionLevel Optimal
Remove-Item $ArchiveDir -Recurse -Force

$SizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
Write-Host ""
Write-Host "Backup complete: $ZipPath ($SizeMB MB)"
