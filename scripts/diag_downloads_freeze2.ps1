$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Downloads folder diagnostic - PASS 2 ==="

# Correct known folder ID for Downloads is 0x15 (CSIDL_DOWNLOADS)
$shell = New-Object -ComObject Shell.Application
$dlFolder = $shell.Namespace(0x15)
Write-Output ("Real Downloads path (known folder 0x15): " + $dlFolder.Self.Path)

# Registry check for the actual Downloads location
Write-Output "`n=== Registry: User Shell Folders (Downloads GUID) ==="
$regPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
$downloadsGuid = '{374DE290-123F-4565-9164-39C4925E467B}'
$val = Get-ItemProperty -Path $regPath -Name $downloadsGuid -ErrorAction SilentlyContinue
if ($val) {
    Write-Output ("Downloads registry value: " + $val.$downloadsGuid)
} else {
    Write-Output "Downloads GUID not found in User Shell Folders (using default)"
}

# --- The suspicious DragonCenter folder ---
Write-Output "`n=== ap_DragonCenter folder contents ==="
$dc = 'C:\Users\Gionie\Downloads\ap_DragonCenterv2.6.2005.0601_2.6.2005.0601_0xfc56f0a8'
if (Test-Path $dc) {
    Get-ChildItem -Path $dc -Recurse -Force | Select-Object -First 60 | ForEach-Object {
        '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.FullName
    }
} else {
    Write-Output "Not found"
}

# --- token_extractor.exe details ---
Write-Output "`n=== token_extractor.exe details ==="
$te = 'C:\Users\Gionie\Downloads\token_extractor.exe'
if (Test-Path $te) {
    $f = Get-Item $te
    Write-Output ("Size: " + $f.Length + " bytes")
    Write-Output ("Created: " + $f.CreationTime)
    Write-Output ("Modified: " + $f.LastWriteTime)
    $vi = $f.VersionInfo
    Write-Output ("FileDescription: " + $vi.FileDescription)
    Write-Output ("ProductName: " + $vi.ProductName)
    Write-Output ("CompanyName: " + $vi.CompanyName)
    Write-Output ("OriginalFilename: " + $vi.OriginalFilename)
    Write-Output ("FileVersion: " + $vi.FileVersion)
    Write-Output ("Signed: " + ($vi.Signature -ne $null))
} else {
    Write-Output "Not found"
}

# --- Media files that trigger thumbnail generation (top 30) ---
Write-Output "`n=== Media files (video/image/audio) that trigger thumbnails ==="
$d = 'C:\Users\Gionie\Downloads'
$media = Get-ChildItem -Path $d -Recurse -Force -File | Where-Object {
    $_.Extension -match '\.(mp4|mkv|avi|mov|wmv|flv|webm|mp3|wav|flac|jpg|jpeg|png|gif|bmp|webp|heic|raw|cr2|nef|dng)$'
}
Write-Output ("Media file count: " + @($media).Count)
$media | Sort-Object Length -Descending | Select-Object -First 30 | ForEach-Object {
    '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.FullName
}

# --- Corrupt thumbnail cache check: list the cache files ---
Write-Output "`n=== Explorer thumbnail cache files ==="
$thumbCache = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
Get-ChildItem -Path $thumbCache -File -ErrorAction SilentlyContinue | Sort-Object Length -Descending | Select-Object -First 15 | ForEach-Object {
    '{0,10:F2} MB  {1}' -f ($_.Length / 1MB), $_.Name
}

# --- Check if Defender real-time scan is on (can cause folder-open hangs) ---
Write-Output "`n=== Windows Defender status ==="
try {
    $status = Get-MpComputerStatus -ErrorAction Stop
    Write-Output ("RealTimeProtectionEnabled: " + $status.RealTimeProtectionEnabled)
    Write-Output ("AntivirusEnabled: " + $status.AntivirusEnabled)
    Write-Output ("AntivirusSignatureLastUpdated: " + $status.AntivirusSignatureLastUpdated)
} catch {
    Write-Output "Get-MpComputerStatus failed (may need admin or not available): $($_.Exception.Message)"
}

# --- Check for .git / node_modules / huge project dirs inside Downloads ---
Write-Output "`n=== Project-like dirs inside Downloads (node_modules/.git) ==="
$projDirs = Get-ChildItem -Path $d -Recurse -Force -Directory -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq 'node_modules' -or $_.Name -eq '.git' -or $_.Name -eq 'venv' -or $_.Name -eq '.venv'
}
Write-Output ("Project dir count: " + @($projDirs).Count)
$projDirs | Select-Object -First 20 | ForEach-Object { $_.FullName }

# --- Top-level folders with their recursive sizes (top 15) ---
Write-Output "`n=== Top-level subfolders by recursive size ==="
Get-ChildItem -Path $d -Force -Directory | ForEach-Object {
    $sub = Get-ChildItem -Path $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue
    $sz = ($sub | Measure-Object -Property Length -Sum).Sum
    [PSCustomObject]@{ Name = $_.Name; SizeMB = [math]::Round($sz / 1MB, 2); Files = @($sub).Count }
} | Sort-Object SizeMB -Descending | Select-Object -First 15 | ForEach-Object {
    '{0,10:F2} MB  {1,6} files  {2}' -f $_.SizeMB, $_.Files, $_.Name
}

Write-Output "`n=== Diagnostic pass 2 complete ==="