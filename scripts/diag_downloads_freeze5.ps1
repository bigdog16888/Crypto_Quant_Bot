$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Explorer crash/restart check (pass 5) ==="

# Application error events (1000 = app error/crash, 1001 = WER, 1002 = app hang)
$logs = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001,1002} -MaxEvents 20
if ($logs) {
    Write-Output ("Found " + @($logs).Count + " crash/hang events:")
    $logs | ForEach-Object {
        '{0}  EventID={1}  Provider={2}' -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id, $_.ProviderName
    }
} else {
    Write-Output "No crash/hang events found (IDs 1000/1001/1002)"
}

Write-Output ""

# Explorer-specific crash events (Event ID 1000 with explorer.exe)
$explorerCrash = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} -MaxEvents 50 -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'explorer\.exe' } | Select-Object -First 10
if ($explorerCrash) {
    Write-Output ("Explorer.exe crash events: " + @($explorerCrash).Count)
    $explorerCrash | ForEach-Object {
        '{0}  EventID={1}' -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id
    }
} else {
    Write-Output "No explorer.exe crash events found in last 50 EventID-1000 records"
}

Write-Output ""

# Check if ShellExperienceHost or search/indexer is also crashing
$shellHostCrash = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} -MaxEvents 50 -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'ShellExperienceHost|SearchIndexer|RuntimeBroker' } | Select-Object -First 10
if ($shellHostCrash) {
    Write-Output ("ShellExperienceHost/SearchIndexer crash events: " + @($shellHostCrash).Count)
    $shellHostCrash | ForEach-Object {
        '{0}  EventID={1}  Provider={2}' -f $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'), $_.Id, $_.ProviderName
    }
} else {
    Write-Output "No ShellExperienceHost/SearchIndexer/RuntimeBroker crashes in last 50 records"
}

Write-Output ""

# ExplorerStartupLog.etl - size and last modified (large = repeated explorer restarts)
$etl = Get-Item "$env:LOCALAPPDATA\Microsoft\Windows\Explorer\ExplorerStartupLog.etl" -ErrorAction SilentlyContinue
if ($etl) {
    Write-Output ("ExplorerStartupLog.etl: " + [math]::Round($etl.Length/1KB,1) + " KB, modified " + $etl.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Output ("  (Normal sizes are only a few KB per boot; larger + recent = explorer restarting repeatedly)")
} else {
    Write-Output "ExplorerStartupLog.etl not found"
}

Write-Output ""

# Search indexer status
Write-Output "=== Windows Search indexer status ==="
$wsearch = Get-Service WSearch -ErrorAction SilentlyContinue
if ($wsearch) {
    Write-Output ("WSearch service: " + $wsearch.Status)
} else {
    Write-Output "WSearch service not found"
}

# Quick check: number of explorer processes (should be 1)
$explorers = @(Get-Process explorer -ErrorAction SilentlyContinue)
Write-Output ("Explorer process count: " + $explorers.Count)

Write-Output "`n=== Pass 5 complete ==="