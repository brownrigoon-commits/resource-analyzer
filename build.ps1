[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$VenvPath = (Join-Path $PSScriptRoot '.venv-build')
)
$ErrorActionPreference = 'Stop'
$VenvPython = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw 'Build Python environment creation failed.' }
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
Push-Location $PSScriptRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $PSScriptRoot 'build\pyinstaller') (Join-Path $PSScriptRoot 'ResourceAnalyzer.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
    $Executable = Join-Path $PSScriptRoot 'dist\ResourceAnalyzer.exe'
    [pscustomobject]@{
        Executable = $Executable
        Bytes = (Get-Item -LiteralPath $Executable).Length
        SHA256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Executable).Hash
    }
} finally {
    Pop-Location
}
