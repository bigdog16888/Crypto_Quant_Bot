$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Step-by-step freeze fix ==="
Write-Output ""

# 1) Verify paddleocr is gone
$dl = 'C:\Users\Gionie\Downloads'
$paddle = Join-Path $dl 'paddleocr_training_data'
Write-Output ("[1] paddleocr_training_data still in Downloads: " + (Test-Path $paddle))

# 2) Current Downloads state
$dlFiles = Get-ChildItem -Path $dl -Recurse -Force -File
Write-Output ("[2] Downloads now: " + @($dlFiles).Count + " files, " + [math]::Round((($dlFiles | Measure-Object Length -Sum).Sum)/1MB, 2) + " MB")
$dlMedia = @($dlFiles | Where-Object { $_.Extension -match '\.(jpg|jpeg|png|gif|bmp|webp|mp4|mkv|avi|mov|mp3)$' })
Write-Output ("    Media files remaining: " + @($dlMedia).Count)

# 3) Check if Dragon Center shell extension is registered
Write-Output ""
Write-Output "[3] Checking MSI/Dragon Center shell extensions..."
$regPaths = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved'
)
$foundExt = $false
foreach ($rp in $regPaths) {
    if (Test-Path $rp) {
        Get-ItemProperty -Path $rp -ErrorAction SilentlyContinue | Get-Member -MemberType NoteProperty | ForEach-Object {
            $val = (Get-ItemProperty -Path $rp -Name $_.Name -ErrorAction SilentlyContinue).$($_.Name)
            if ($val -match 'MSI|Dragon|Mystic|LED|OneDC') {
                Write-Output ("    FOUND: " + $val + "  (GUID=" + $_.Name + ")")
                $foundExt = $true
            }
        }
    }
}
if (-not $foundExt) {
    Write-Output "    No MSI/Dragon Center shell extensions registered"
}

# 4) Check Dragon Center services status
Write-Output ""
Write-Output "[4] Dragon Center / MSI services:"
$svcNames = @('MSIAPService', 'MSI_SERVICE', 'OneDC', 'DragonCenter', 'LEDKeeper')
foreach ($svc in $svcNames) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        Write-Output ("    " + $s.Name + " (" + $s.DisplayName + "): " + $s.Status + "  StartType=" + $s.StartType)
    }
}
# Check for any service with MSI in name
Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'MSI|Dragon|Mystic' } | ForEach-Object {
    Write-Output ("    " + $_.Name + " (" + $_.DisplayName + "): " + $_.Status + "  StartType=" + $_.StartType)
}

# 5) Kill explorer and clear thumbnail cache
Write-Output ""
Write-Output "[5] Attempting to clear thumbnail cache..."
$thumbCache = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"
if (Test-Path $thumbCache) {
    # Don't actually kill explorer here - just report what needs to be done
    Write-Output "    Thumbnail cache dir: $thumbCache"
    $thumbFiles = Get-ChildItem -Path $thumbCache -File | Where-Object { $_.Name -match 'thumbcache_|iconcache_' }
    Write-Output ("    Cache files to clear: " + @($thumbFiles).Count)
    $thumbFiles | Sort-Object Length -Descending | Select-Object -First 5 | ForEach-Object {
        Write-Output ("      " + $_.Name + " (" + [math]::Round($_.Length/1MB, 2) + " MB)")
    }
}

Write-Output ""
Write-Output "=== Diagnosis complete ==="
Write-Output ""
Write-Output "=== RECOMMENDED FIX ==="
Write-Output "Run these commands as Administrator to fix the freeze:"
Write-Output ""
Write-Output "STEP A - Stop the crashing Dragon Center services:"
Write-Output '  sc stop MSIAPService'
Write-Output '  sc config MSIAPService start=disabled'
Write-Output ""
Write-Output "STEP B - Clear the stale thumbnail cache:"
Write-Output '  taskkill /f /im explorer.exe'
Write-Output ('  del /q "' + $thumbCache + '\thumbcache_*.db"')
Write-Output ('  del /q "' + $thumbCache + '\iconcache_*.db"')
Write-Output '  start explorer'
Write-Output ""
Write-Output "STEP C - If still broken, remove the Dragon Center shell extension:"
Write-Output '  reg delete "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Shell Extensions\Approved" /v {GUID} /f'
Write-Output "  (Replace {GUID} with the one found in step 3 above)"