$ErrorActionPreference = 'SilentlyContinue'

Write-Output "=== Pass 6: Which process is hanging/crashing? ==="

# Application Hang events with full details (today)
Write-Output "`n--- EventID 1002 (Application Hang) today ---"
$hangs = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1002; StartTime=(Get-Date).Date} -ErrorAction SilentlyContinue
if ($hangs) {
    Write-Output ("Hang events today: " + @($hangs).Count)
    $hangs | ForEach-Object {
        $msg = $_.Message
        Write-Output ("`n>>> " + $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))
        # Extract the key lines: program name, version, hang description
        $msg -split "`r?`n" | Where-Object { $_ -match 'Program|Application|Description|Path|Module|Fault|Hung|Version' } | Select-Object -First 8 | ForEach-Object { "    " + $_ }
    }
} else {
    Write-Output "No hang events today"
}

# Application Error events with full details (today)
Write-Output "`n--- EventID 1000 (Application Error) today ---"
$errs = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=(Get-Date).Date} -ErrorAction SilentlyContinue
if ($errs) {
    Write-Output ("Error events today: " + @($errs).Count)
    $errs | ForEach-Object {
        $msg = $_.Message
        Write-Output ("`n>>> " + $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'))
        $msg -split "`r?`n" | Where-Object { $_ -match 'Faulting application|Faulting module|Exception|Path' } | Select-Object -First 6 | ForEach-Object { "    " + $_ }
    }
} else {
    Write-Output "No error events today"
}

# Also check yesterday's explorer-related hangs if any
Write-Output "`n--- Any hang/error events mentioning explorer.exe, dllhost, or thumbnails (last 2 days) ---"
$twoDays = (Get-Date).AddDays(-2)
$all = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1002; StartTime=$twoDays} -ErrorAction SilentlyContinue |
    Where-Object { $_.Message -match 'explorer|dllhost|thumb|photo|codec|Shell' }
if ($all) {
    $all | Select-Object -First 10 | ForEach-Object {
        Write-Output ("`n>>> " + $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') + "  EventID=" + $_.Id)
        $_.Message -split "`r?`n" | Where-Object { $_ -match 'Program|Application|Description|Path|Hung' } | Select-Object -First 5 | ForEach-Object { "    " + $_ }
    }
} else {
    Write-Output "No explorer/dllhost/thumbnail-related hang or error events in last 2 days"
}

Write-Output "`n=== Pass 6 complete ==="