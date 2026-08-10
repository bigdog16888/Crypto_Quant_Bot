$ErrorActionPreference = 'SilentlyContinue'

$d = 'C:\Users\Gionie\Downloads'
$out = 'C:\Users\Gionie\Documents\GitHub\Crypto_Quant_Bot\scripts\downloads_diag_report.txt'

$report = New-Object System.Collections.Generic.List[string]
function Log($msg) { $script:report.Add($msg) }

Log "=== Downloads folder diagnostic - CLEAN REPORT (pass 4) ==="
Log ("Timestamp: " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

# Exact per-directory breakdown (direct children of each dir, files only, no recursion in count)
Log "`n--- Per-directory file counts (direct children files only) ---"
$totalFiles = 0
Get-ChildItem -Path $d -Force -Directory | ForEach-Object {
    $dirName = $_.Name
    $files = @(Get-ChildItem -Path $_.FullName -Force -File)
    $totalFiles += $files.Count
    Log ("{0,6} files  {1}" -f $files.Count, $dirName)
}
$topFiles = @(Get-ChildItem -Path $d -Force -File)
$totalFiles += $topFiles.Count
Log ("{0,6} files  [TOP-LEVEL FILES]" -f $topFiles.Count)
Log ("TOTAL direct-child files: " + $totalFiles)

# Recursive total
$allFiles = Get-ChildItem -Path $d -Recurse -Force -File
Log ("`nRECURSIVE TOTAL FILES: " + @($allFiles).Count)

# Media count - CLEAN
$mediaExt = '\.(mp4|mkv|avi|mov|wmv|flv|webm|mp3|wav|flac|jpg|jpeg|png|gif|bmp|webp|heic|raw|cr2|nef|dng)$'
$media = $allFiles | Where-Object { $_.Extension -match $mediaExt }
Log ("MEDIA FILES (recursive): " + @($media).Count)

# Where are the media files concentrated? Per-directory media count
Log "`n--- Media files per directory (top 15 dirs) ---"
Get-ChildItem -Path $d -Force -Directory | ForEach-Object {
    $m = @(Get-ChildItem -Path $_.FullName -Recurse -Force -File | Where-Object { $_.Extension -match $mediaExt })
    [PSCustomObject]@{ Name = $_.Name; Media = $m.Count }
} | Sort-Object Media -Descending | Select-Object -First 15 | ForEach-Object {
    Log ("{0,6} media  {1}" -f $_.Media, $_.Name)
}

# Largest single directories by file count (top 10)
Log "`n--- Largest directories by recursive file count (top 10) ---"
Get-ChildItem -Path $d -Force -Directory | ForEach-Object {
    $c = @(Get-ChildItem -Path $_.FullName -Recurse -Force -File)
    [PSCustomObject]@{ Name = $_.Name; Files = $c.Count }
} | Sort-Object Files -Descending | Select-Object -First 10 | ForEach-Object {
    Log ("{0,6} files  {1}" -f $_.Files, $_.Name)
}

# Thumbnail cache precise
Log "`n--- Thumbnail cache ---"
$thumbCache = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
$thumbFiles = Get-ChildItem -Path $thumbCache -File
Log ("Cache file count: " + @($thumbFiles).Count)
Log ("Cache total size (MB): " + [math]::Round((($thumbFiles | Measure-Object Length -Sum).Sum)/1MB, 2))
$thumbFiles | Sort-Object Length -Descending | ForEach-Object {
    Log ("  {0,7:F2} MB  {1}" -f ($_.Length/1MB), $_.Name)
}

# Process check: is explorer running / any process with Downloads in path
Log "`n--- Processes (potential lock/write on Downloads) ---"
Get-Process | Where-Object {
    $_.Path -like "$d*"
} | ForEach-Object {
    Log ("  RUNNING FROM DOWNLOADS: " + $_.ProcessName + " (PID " + $_.Id + ") path=" + $_.Path)
}

# Check explorer CPU/threads (hung indicator)
$explorer = Get-Process explorer -ErrorAction SilentlyContinue | Select-Object -First 1
if ($explorer) {
    Log ("Explorer PID: " + $explorer.Id + "  Responding: " + $explorer.Responding + "  Threads: " + $explorer.Threads.Count + "  WorkingSetMB: " + [math]::Round($explorer.WorkingSet64/1MB,1))
}

$report | Out-File -FilePath $out -Encoding utf8
Write-Output ("Report written to: " + $out)
Write-Output ("Line count: " + $report.Count)