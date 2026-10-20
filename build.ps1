[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$VenvPath,
    [string]$SensorArchive,
    [string]$PawnIOInstaller
)
$ErrorActionPreference = 'Stop'
if (-not $VenvPath) { $VenvPath = Join-Path $PSScriptRoot '.venv-build' }
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
& (Join-Path $PSScriptRoot 'setup.ps1') -Python $Python -VenvPath $VenvPath -Build -SensorArchive $SensorArchive -PawnIOInstaller $PawnIOInstaller
Push-Location $PSScriptRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $PSScriptRoot 'build\pyinstaller') (Join-Path $PSScriptRoot 'ResourceAnalyzer.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
    $InstallersSource = Join-Path $PSScriptRoot 'installers'
    $InstallersDestination = Join-Path $PSScriptRoot 'dist\installers'
    if (-not (Test-Path -LiteralPath (Join-Path $InstallersSource 'PawnIO_setup.exe') -PathType Leaf)) {
        throw 'The redistributable PawnIO package is missing from installers.'
    }
    New-Item -ItemType Directory -Path $InstallersDestination -Force | Out-Null
    Get-ChildItem -LiteralPath $InstallersSource -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $InstallersDestination $_.Name) -Force
    }
    $Executable = Join-Path $PSScriptRoot 'dist\ResourceAnalyzer.exe'
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash.ToLowerInvariant()
    "$Hash  ResourceAnalyzer.exe" | Set-Content (Join-Path $PSScriptRoot 'dist\SHA256SUMS.txt') -Encoding ascii
    $SourceCommit = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not record the source commit.' }
    $WorkingChanges = & git status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0) { throw 'Could not record the source working state.' }
    $AppVersion = & $VenvPython -c 'from app_paths import VERSION; print(VERSION)'
    if ($LASTEXITCODE -ne 0) { throw 'Could not record the app version.' }
    [ordered]@{
        app_version = "$AppVersion"
        source_commit = "$SourceCommit"
        uncommitted_changes = [bool]$WorkingChanges
        built_at_utc = [DateTime]::UtcNow.ToString('o')
        executable_sha256 = $Hash
    } | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot 'dist\BUILD-INFO.txt') -Encoding utf8
    & $VenvPython -m pip freeze | Set-Content (Join-Path $PSScriptRoot 'dist\BUILD-DEPENDENCIES.txt') -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Could not record build dependencies.' }
    [pscustomobject]@{
        Executable = $Executable
        Bytes = (Get-Item -LiteralPath $Executable).Length
        SHA256 = $Hash
    }
} finally {
    Pop-Location
}
