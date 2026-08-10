$ErrorActionPreference = 'SilentlyContinue'

$cacheDir = "$env:LOCALAPPDATA\Microsoft\Windows\Explorer"

Write-Output "=== Clearing thumbnail cache ==="
Write-Output ("Cache dir: " + $cacheDir)

# Remove thumbnail and icon cache files
$removed = 0
Get-ChildItem -Path $cacheDir -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(thumbcache_|iconcache_).*\.db$'
} | ForEach-Object {
    Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $_.FullName)) {
        $removed++
        Write-Output ("  Removed: " + $_.Name)
    } else {
        Write-Output ("  FAILED to remove (locked): " + $_.Name)
    }
}

Write-Output ("Total cache files removed: " + $removed)

# Verify remaining
$remaining = @(Get-ChildItem -Path $cacheDir -File -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match '^(thumbcache_|iconcache_).*\.db$'
})
Write-Output ("Cache files remaining: " + @($remaining).Count)

Write-Output "=== Done ==="