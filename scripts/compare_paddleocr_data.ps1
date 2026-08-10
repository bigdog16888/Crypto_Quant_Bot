$ErrorActionPreference = 'SilentlyContinue'

$downloadsData = 'C:\Users\Gionie\Downloads\paddleocr_training_data'

Write-Output "=== Compare Downloads training_data vs other locations ==="

# Baseline: what's in Downloads\paddleocr_training_data?
Write-Output "`n--- BASELINE: Downloads\paddleocr_training_data ---"
if (Test-Path $downloadsData) {
    $dlFiles = Get-ChildItem -Path $downloadsData -Recurse -Force -File
    Write-Output ("Files: " + @($dlFiles).Count)
    Write-Output ("Size MB: " + [math]::Round((($dlFiles | Measure-Object Length -Sum).Sum)/1MB, 2))
    Write-Output "Top-level contents:"
    Get-ChildItem -Path $downloadsData -Force | ForEach-Object {
        '{0,10:N0}  {1}' -f $_.Length, $_.Name
    }
    # Sample of image subfolder
    $imgDir = Join-Path $downloadsData 'images'
    if (Test-Path $imgDir) {
        $imgFiles = Get-ChildItem -Path $imgDir -Force -File
        Write-Output ("`nimages/ subfolder: " + @($imgFiles).Count + " files")
        $imgFiles | Select-Object -First 5 | ForEach-Object { "  " + $_.Name + "  (" + $_.Length + " bytes)" }
    }
} else {
    Write-Output "NOT FOUND - already moved/deleted"
}

# Candidate locations to compare
$candidates = @(
    'C:\paddleocr_training',
    'C:\Users\Gionie\my-fastapi-paddleocr',
    'C:\Users\Gionie\.paddleocr',
    'C:\tmp\OCR_Accounting_Tool'
)

foreach ($cand in $candidates) {
    Write-Output ("`n--- CANDIDATE: " + $cand + " ---")
    if (-not (Test-Path $cand)) {
        Write-Output "  Not found"
        continue
    }
    $files = Get-ChildItem -Path $cand -Recurse -Force -File
    Write-Output ("  Files: " + @($files).Count)
    Write-Output ("  Size MB: " + [math]::Round((($files | Measure-Object Length -Sum).Sum)/1MB, 2))
    Write-Output "  Top-level:"
    Get-ChildItem -Path $cand -Force | Select-Object -First 15 | ForEach-Object {
        '    {0,10:N0}  {1}' -f $_.Length, $_.Name
    }
    # Does it contain an 'images' dir with many jpgs? (signature of the training data)
    $nestedJpgs = @($files | Where-Object { $_.Extension -eq '.jpg' })
    Write-Output ("  Nested .jpg count: " + $nestedJpgs.Count)
        
    # Search for any dir matching training*data
    $sub = Get-ChildItem -Path $cand -Recurse -Directory -Force | Where-Object { $_.Name -match 'training|train_data|dataset|images' } | Select-Object -First 10
    $sub | ForEach-Object { Write-Output ("  Subdir match: " + $_.FullName) }
}

Write-Output "`n=== Compare complete ==="