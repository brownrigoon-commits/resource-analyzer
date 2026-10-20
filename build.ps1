[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$VenvPath = (Join-Path $PSScriptRoot '.venv-build'),
    [string]$SensorArchive,
    [string]$PawnIOInstaller
)
$ErrorActionPreference = 'Stop'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
& (Join-Path $PSScriptRoot 'setup.ps1') -Python $Python -VenvPath $VenvPath -Build -SensorArchive $SensorArchive -PawnIOInstaller $PawnIOInstaller
Push-Location $PSScriptRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $PSScriptRoot 'build\pyinstaller') (Join-Path $PSScriptRoot 'ResourceAnalyzer.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
    $Executable = Join-Path $PSScriptRoot 'dist\ResourceAnalyzer.exe'
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash.ToLowerInvariant()
    "$Hash  ResourceAnalyzer.exe" | Set-Content (Join-Path $PSScriptRoot 'dist\SHA256SUMS.txt') -Encoding ascii
    [pscustomobject]@{
        Executable = $Executable
        Bytes = (Get-Item -LiteralPath $Executable).Length
        SHA256 = $Hash
    }
} finally {
    Pop-Location
}
