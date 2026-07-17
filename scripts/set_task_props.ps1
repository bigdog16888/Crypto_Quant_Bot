$t = Get-ScheduledTask -TaskName 'CQB_SessionStartCheck'
$t.Settings.StartWhenAvailable = $true
$t.Settings.DisallowStartIfOnBatteries = $false
$t.Settings.StopIfGoingOnBatteries = $false
Set-ScheduledTask -InputObject $t | Out-Null
$u = Get-ScheduledTask -TaskName 'CQB_SessionStartCheck'
Write-Host ("StartWhenAvailable=" + $u.Settings.StartWhenAvailable)
Write-Host ("DisallowStartIfOnBatteries=" + $u.Settings.DisallowStartIfOnBatteries)
Write-Host ("State=" + $u.State)
