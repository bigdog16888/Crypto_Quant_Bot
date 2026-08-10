$ErrorActionPreference = 'SilentlyContinue'

$d = 'C:\Users\Gionie\Downloads'

if (-not (Test-Path $d)) {
    Write-Output "Downloads folder NOT found at: $d"
    exit 1
}

Write-Output "=== Downloads folder diagnostic ==="
Write-Output ("Path: " + $d)

# Top-level item count
$files = Get-ChildItem -Path $d -Force
Write-Output ("Top-level item count: " + @($files).Count)

# Total recursive file size and file count
$allFiles = Get-ChildItem -Path $d -Recurse -Force -File
Write-Output ("Recursive file count: " + @($allFiles).Count)
$totalBytes = ($allFiles | Measure-Object -Property Length -Sum).Sum
Write-Output ("Total recursive size (MB): " + [math]::Round($totalBytes / 1MB, 2))

# Is there a OneDrive redirection? Check the real folder path
$shell = New-Object -ComObject Shell.Application
$dlFolder = $shell.Namespace(0x5)  # Downloads known folder
Write-Output ("Shell Downloads path: " + $dlFolder.Self.Path)
Write-Output ("Redirected? (differs from C:\Users\Gionie\Downloads): " + ($dlFolder.Self.Path -ne $d))

# --- Large files (top 20 by size) ---
Write-Output "`n=== Top 20 largest files ==="
$allFiles | Sort-Object Length -Descending | Select-Object -First 20 | ForEach-Object {
    '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.FullName
}

# --- Suspicious / degenerate files ---
Write-Output "`n=== Suspicious / degenerate files ==="
$zeroBytes = @($allFiles | Where-Object { $_.Length -eq 0 })
Write-Output ("Zero-byte files: " + $zeroBytes.Count)
$zeroDirs = @(Get-ChildItem -Path $d -Recurse -Force -Directory)
Write-Output ("Directory count: " + @($zeroDirs).Count)

# Empty directories
$emptyDirs = @($zeroDirs | Where-Object { @(Get-ChildItem -Path $_.FullName -Force).Count -eq 0 })
Write-Output ("Empty directories: " + @($emptyDirs).Count)

# Files with weird names (very long, or containing invalid/odd chars)
Write-Output "`n=== Files with very long names (>200 chars) ==="
$allFiles | Where-Object { $_.Name.Length -gt 200 } | Select-Object -First 20 | ForEach-Object { $_.FullName }

# Files with odd extensions that could confuse Explorer (e.g. .tmp, .crdownload, .partial)
Write-Output "`n=== Files with suspicious extensions ==="
$allFiles | Where-Object { $_.Extension -match '\.(tmp|crdownload|partial|part|download|!ut|opdownload)$' } | Select-Object -First 30 | ForEach-Object { $_.FullName }

# --- Check for processes actively writing to Downloads ---
Write-Output "`n=== Processes with open handles in Downloads (via handle.exe if available) ==="
$handleExe = Get-Command handle.exe -ErrorAction SilentlyContinue
if ($handleExe) {
    & $handleExe -accepteula -nobanner $d 2>&1 | Select-Object -First 40
} else {
    Write-Output "handle.exe not installed - skipping open-handle check. (Sysinternals handle.exe would show which process is locking files.)"
}

# --- Check thumbnail cache / Explorer state ---
Write-Output "`n=== Thumbnail cache size ==="
$thumbCache = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
if (Test-Path $thumbCache) {
    $thumbFiles = Get-ChildItem -Path $thumbCache -File -ErrorAction SilentlyContinue
    $thumbBytes = ($thumbFiles | Measure-Object -Property Length -Sum).Sum
    Write-Output ("Explorer cache dir size (MB): " + [math]::Round($thumbBytes / 1MB, 2))
    Write-Output ("Explorer cache file count: " + @($thumbFiles).Count)
} else {
    Write-Output "Explorer cache dir not found"
}

# --- Check for reparse points / symlinks / junctions (can cause hangs) ---
Write-Output "`n=== Reparse points (symlinks/junctions) in Downloads ==="
$reparse = @($allFiles + $zeroDirs | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint })
Write-Output ("Reparse point count: " + @($reparse).Count)
$reparse | Select-Object -First 20 | ForEach-Object { $_.FullName }

# --- Check for files with invalid/control characters in name ---
Write-Output "`n=== Files with control characters in name ==="
$ctrl = @($allFiles | Where-Object { $_.Name -match '[\x00-\x1f]' })
Write-Output ("Control-char name count: " + @($ctrl).Count)
$ctrl | Select-Object -First 20 | ForEach-Object { $_.FullName }

Write-Output "`n=== Diagnostic complete ==="