[CmdletBinding()]
param([string]$PackageDirectory = $PSScriptRoot, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$Executable = Join-Path $PackageDirectory 'ResourceAnalyzer.exe'
$Info = Get-Content -LiteralPath (Join-Path $PackageDirectory 'BUILD-INFO.txt') -Raw | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash -ne $Info.executable_sha256) {
    throw 'The executable does not match BUILD-INFO.txt. Nothing was changed.'
}
$Health = $null
try { $Health = Invoke-RestMethod 'http://127.0.0.1:7442/api/health' -TimeoutSec 3 }
catch { if (Get-NetTCPConnection -State Listen -LocalPort 7442 -ErrorAction SilentlyContinue) { throw 'Port 7442 is occupied by an unidentified application. Nothing was changed.' } }
if ($Health -and $Health.app_id -ne 'resource-analyzer') { throw 'The running application is not Resource Analyzer. Nothing was changed.' }
if ($Health) {
    $Governor = Invoke-RestMethod 'http://127.0.0.1:7442/api/governor' -TimeoutSec 3
    if ($null -ne $Governor.active) { throw 'An allocated worker is active. Wait for it to finish, then run this update again.' }
}
if ($CheckOnly) {
    [pscustomobject]@{ VerifiedVersion = $Info.app_version; SourceCommit = $Info.source_commit; CurrentVersion = $Health.version }
    return
}
if ($Health -and $Health.version -eq $Info.app_version -and 'qwen-vision' -in $Governor.adapters.stage) {
    Write-Host 'The new resource controller is already running.'
    Start-Process 'http://127.0.0.1:7442/dashboard'
    return
}
Write-Host 'Updating only Resource Analyzer. Existing history and policy settings are retained.'
if ($Health) {
    & $Executable --stop --no-browser
    if ($LASTEXITCODE -ne 0) { throw 'The normal shutdown request failed. No process was killed.' }
    $Deadline = (Get-Date).AddSeconds(30)
    while (Get-NetTCPConnection -State Listen -LocalPort 7442 -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $Deadline) { throw 'Normal shutdown is still pending. No process was killed.' }
        Start-Sleep -Milliseconds 250
    }
}
Start-Process -FilePath $Executable -ArgumentList '--autostart','--no-browser' -WorkingDirectory $PackageDirectory -WindowStyle Hidden
$Deadline = (Get-Date).AddSeconds(30)
do {
    try { $Current = Invoke-RestMethod 'http://127.0.0.1:7442/api/health' -TimeoutSec 2; break }
    catch { if ((Get-Date) -gt $Deadline) { throw 'The new analyzer did not respond. Run ResourceAnalyzer.exe manually and inspect its local log.' }; Start-Sleep -Milliseconds 300 }
} while ($true)
if ($Current.version -ne $Info.app_version) { throw 'The response version does not match the selected package.' }
Write-Host ('Resource Analyzer ' + $Current.version + ' is running. Existing history was retained.')
Start-Process 'http://127.0.0.1:7442/dashboard'
