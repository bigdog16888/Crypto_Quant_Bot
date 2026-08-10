$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Search for paddleocr folders across all drives ==="
Write-Output ("Time: " + (Get-Date -Format 'HH:mm:ss'))

$targets = @('paddleocr_training_data', 'paddleocr', 'PaddleOCR', 'paddleocr_training_data*')

$drives = @('C:\', 'D:\', 'G:\')

$found = New-Object System.Collections.Generic.List[string]

foreach ($drive in $drives) {
    if (-not (Test-Path $drive)) { continue }
    Write-Output ("`n--- Searching " + $drive + " (this may take a minute) ---")
    Get-ChildItem -Path $drive -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^paddleocr.*training.*data$|^paddleocr$|^PaddleOCR$' -or $_.Name -like '*paddleocr*' } |
        ForEach-Object {
            $fullPath = $_.FullName
            $sizeMB = [math]::Round(($(Get-ChildItem -Path $fullPath -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum) / 1MB, 2)
            $count = @(Get-ChildItem -Path $fullPath -Recurse -Force -File -ErrorAction SilentlyContinue).Count
            Write-Output ("FOUND: " + $fullPath + "  (" + $count + " files, " + $sizeMB + " MB)")
            $found.Add($fullPath + "|" + $count + "|" + $sizeMB)
        }
}

Write-Output "`n=== Search complete ==="
if ($found.Count -eq 0) {
    Write-Output "No duplicates found anywhere."
} else {
    Write-Output "Matches found:"
    $found | ForEach-Object { Write-Output ("  " + $_) }
}