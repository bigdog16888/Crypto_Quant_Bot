$ErrorActionPreference = 'SilentlyContinue'

$dest = 'C:\Users\Gionie\Documents\paddleocr_training_data'
$dl = 'C:\Users\Gionie\Downloads'

Write-Output "=== Verify move ==="

# Destination intact?
if (Test-Path $dest) {
    $files = Get-ChildItem -Path $dest -Recurse -Force -File
    Write-Output ("Dest file count: " + @($files).Count)
    Write-Output ("Dest size MB: " + [math]::Round((($files | Measure-Object Length -Sum).Sum)/1MB, 2))
    $imgDir = Join-Path $dest 'images'
    if (Test-Path $imgDir) {
        $imgFiles = Get-ChildItem -Path $imgDir -Force -File
        Write-Output ("Dest images/ count: " + @($imgFiles).Count)
    }
} else {
    Write-Output "DESTINATION MISSING - move failed!"
}

# Source gone?
Write-Output ("Source still exists: " + (Test-Path (Join-Path $dl 'paddleocr_training_data')))

# Downloads now lighter?
$dlFiles = Get-ChildItem -Path $dl -Recurse -Force -File
Write-Output ("Downloads now: " + @($dlFiles).Count + " files, " + [math]::Round((($dlFiles | Measure-Object Length -Sum).Sum)/1MB, 2) + " MB")
$dlMedia = @($dlFiles | Where-Object { $_.Extension -match '\.(jpg|jpeg|png|gif|bmp|webp|mp4|mkv|avi|mov|mp3)$' })
Write-Output ("Downloads media files now: " + @($dlMedia).Count)

Write-Output "`n=== Verify complete ==="