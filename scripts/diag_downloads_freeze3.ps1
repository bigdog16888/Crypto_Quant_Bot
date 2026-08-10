$ErrorActionPreference = 'SilentlyContinue'

$d = 'C:\Users\Gionie\Downloads'

Write-Output "=== Downloads folder diagnostic - PASS 3 (enumeration integrity) ==="

# 1) Exact recursive file count, counting errors per directory
Write-Output "`n=== Recursive enumeration with error tracking ==="
$totalFiles = 0
$totalDirs = 0
$errorDirs = @()

$dirs = Get-ChildItem -Path $d -Recurse -Force -Directory
$totalDirs = @($dirs).Count
Write-Output ("Total directories: " + $totalDirs)

foreach ($dirObj in $dirs) {
    $children = @(Get-ChildItem -Path $dirObj.FullName -Force -ErrorAction SilentlyContinue)
    $totalFiles += @($children | Where-Object { -not $_.PSIsContainer }).Count
}

# Top-level files
$topFiles = @(Get-ChildItem -Path $d -Force -File)
$totalFiles += $topFiles.Count
Write-Output ("Exact recursive file count (manual walk): " + $totalFiles)

# Try to detect enumeration failures: compare with a raw .NET enumeration
Write-Output "`n=== .NET enumeration check (catches access-denied / broken dirs) ==="
$netCount = 0
$netErrors = @()
try {
    $netCount = [System.IO.Directory]::EnumerateFiles($d, '*', [System.IO.SearchOption]::AllDirectories).Count
    Write-Output (".NET EnumerateFiles count: " + $netCount)
} catch {
    Write-Output ("EnumerateFiles threw: " + $_.Exception.Message)
}

# --- Files with path length > 260 (MAX_PATH issues) ---
Write-Output "`n=== Files with full path > 260 chars ==="
$longPaths = @()
Get-ChildItem -Path $d -Recurse -Force -File | ForEach-Object {
    if ($_.FullName.Length -gt 260) {
        $longPaths += $_.FullName
    }
}
Write-Output ("Long-path files: " + @($longPaths).Count)
$longPaths | Select-Object -First 20 | ForEach-Object { $_.Substring(0, [Math]::Min($_.Length, 200)) }

# --- UD3_02_PCL6_2201a structure (2558 files - printer driver?) ---
Write-Output "`n=== UD3_02_PCL6_2201a folder structure ==="
$ud = 'C:\Users\Gionie\Downloads\UD3_02_PCL6_2201a'
if (Test-Path $ud) {
    Get-ChildItem -Path $ud -Force | Select-Object -First 30 | ForEach-Object {
        '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.Name
    }
    $udFiles = Get-ChildItem -Path $ud -Recurse -Force -File
    Write-Output ("UD3 file count: " + @($udFiles).Count)
    Write-Output ("UD3 total size (MB): " + [math]::Round((($udFiles | Measure-Object Length -Sum).Sum) / 1MB, 2))
    # Sample extensions
    Write-Output "`nUD3 extension histogram (top 15):"
    $udFiles | Group-Object Extension | Sort-Object Count -Descending | Select-Object -First 15 | ForEach-Object {
        '{0,6}  {1}' -f $_.Count, $_.Name
    }
} else {
    Write-Output "Not found"
}

# --- paddleocr_training_data structure ---
Write-Output "`n=== paddleocr_training_data folder structure ==="
$po = 'C:\Users\Gionie\Downloads\paddleocr_training_data'
if (Test-Path $po) {
    Get-ChildItem -Path $po -Force | Select-Object -First 20 | ForEach-Object {
        '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.Name
    }
    $poFiles = Get-ChildItem -Path $po -Recurse -Force -File
    Write-Output ("paddleocr file count: " + @($poFiles).Count)
    Write-Output "`n--- paddleocr extension distribution (top 15):"
    $poFiles | Group-Object Extension | Sort-Object Count -Descending | Select-Object -First 15 | ForEach-Object {
        '{0,6}  {1}' -f $_.Count, $_.Name
    }
} else {
    Write-Output "Not found"
}

# --- Corrupt media check: sample first 100 media files, verify magic bytes ---
Write-Output "`n=== Media file integrity spot-check (first 100 media files) ==="
$media = Get-ChildItem -Path $d -Recurse -Force -File | Where-Object {
    $_.Extension -match '\.(jpg|jpeg|png|gif|bmp|webp|mp4|mkv|avi|mov|mp3)$'
} | Select-Object -First 100

$bad = 0
foreach ($m in $media) {
    try {
        $fs = [System.IO.File]::OpenRead($m.FullName)
        $buf = New-Object byte[] 16
        $read = $fs.Read($buf, 0, 16)
        $fs.Close()
        if ($read -lt 4) { $bad++; Write-Output ("TOO SMALL: " + $m.FullName); continue }
        $sig = -join ($buf[0..3] | ForEach-Object { $_.ToString('X2') })
        $ok = $false
        switch ($m.Extension.ToLower()) {
            '.jpg'  { $ok = $sig -match '^FFD8' }
            '.jpeg' { $ok = $sig -match '^FFD8' }
            '.png'  { $ok = $sig -match '^89504E47' }
            '.gif'  { $ok = $sig -match '^47494638' }
            '.bmp'  { $ok = $sig -match '^424D' }
            '.webp' { $ok = $sig -match '^52494646' }
            '.mp3'  { $ok = $sig -match '^(494433|FFFB|FFFA|FFF3|FFF2)' }
            '.mp4'  { $ok = $sig -match '^(000000|667479)' }
            default { $ok = $true }
        }
        if (-not $ok) {
            $bad++
            Write-Output ("MISMATCH: " + $m.FullName + "  sig=" + $sig)
        }
    } catch {
        $bad++
        Write-Output ("ERROR reading: " + $m.FullName + "  " + $_.Exception.Message)
    }
}
Write-Output ("Bad/corrupt media files in sample: " + $bad + " / " + @($media).Count)

Write-Output "`n=== Diagnostic pass 3 complete ==="