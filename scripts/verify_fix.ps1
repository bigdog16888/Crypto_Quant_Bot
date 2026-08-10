$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Verify fix (pass 7) ==="

# 1) Explorer running?
$explorer = Get-Process explorer -ErrorAction SilentlyContinue | Select-Object -First 1
if ($explorer) {
    Write-Output ("[1] Explorer running: PID=" + $explorer.Id + "  Responding=" + $explorer.Responding)
} else {
    Write-Output "[1] Explorer NOT running!"
}

# 2) Downloads enumerates cleanly?
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$dlFiles = Get-ChildItem -Path 'C:\Users\Gionie\Downloads' -Recurse -Force -File
$sw.Stop()
Write-Output ("[2] Downloads enumerated: " + @($dlFiles).Count + " files in " + $sw.ElapsedMilliseconds + " ms")

# 3) Any NEW explorer hang events since 09:55 (after cache clear)?
$since = Get-Date '2026-08-10 09:55:00'
$newHangs = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1002; StartTime=$since} -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'explorer\.exe' }
if ($newHangs) {
    Write-Output ("[3] NEW explorer hangs since fix: " + @($newHangs).Count)
    $newHangs | ForEach-Object { "    " + $_.TimeCreated.ToString('HH:mm:ss') }
} else {
    Write-Output "[3] No new explorer.exe hang events since fix (09:55)"
}

# 4) Thumbnail cache rebuilt / still clean?
$cacheDir = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
$cacheFiles = @(Get-ChildItem -Path $cacheDir -File | Where-Object { $_.Name -match '^(thumbcache_|iconcache_).*\.db$' })
Write-Output ("[4] Thumbnail cache files now: " + @($cacheFiles).Count)
$cacheMB = [math]::Round((($cacheFiles | Measure-Object Length -Sum).Sum)/1MB, 2)
Write-Output ("    Cache size now (MB): " + $cacheMB)

Write-Output "`n=== Verify complete ==="