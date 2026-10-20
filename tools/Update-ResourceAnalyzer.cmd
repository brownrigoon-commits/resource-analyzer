@echo off
setlocal
"%~dp0ResourceAnalyzer.exe" --apply-update --autostart
if errorlevel 1 (
  echo Update did not complete. Please keep this message for troubleshooting.
  pause
)
endlocal
