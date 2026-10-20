@echo off
setlocal
powershell.exe -NoLogo -NoProfile -File "%~dp0Update-ResourceAnalyzer.ps1"
if errorlevel 1 (
  echo Update did not complete. Please keep this message for troubleshooting.
  pause
)
endlocal
