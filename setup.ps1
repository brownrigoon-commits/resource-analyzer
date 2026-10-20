[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$VenvPath,
    [switch]$Build,
    [string]$SensorArchive,
    [string]$PawnIOInstaller
)
$ErrorActionPreference = 'Stop'
if (-not $VenvPath) { $VenvPath = Join-Path $PSScriptRoot '.venv' }
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -c "import sys, struct; assert sys.version_info >= (3, 11) and struct.calcsize('P') == 8, 'Python 3.11+ x64 required (3.12 tested)'"
    if ($LASTEXITCODE -ne 0) { throw 'A compatible x64 Python installation is required.' }
    & $Python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
$Requirements = if ($Build) { 'requirements-build.txt' } else { 'requirements.txt' }
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot $Requirements)
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
$SensorArgs = @()
if ($SensorArchive) { $SensorArgs += @('--archive', $SensorArchive) }
if ($PawnIOInstaller) { $SensorArgs += @('--pawnio-installer', $PawnIOInstaller) }
& $VenvPython (Join-Path $PSScriptRoot 'tools\build_sensor_bridge.py') @SensorArgs
if ($LASTEXITCODE -ne 0) { throw 'Sensor package preparation failed.' }
Write-Output "Ready: $VenvPython (no driver installed; no app started)"
