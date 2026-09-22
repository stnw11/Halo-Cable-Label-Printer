@echo off
rem Double-click this to update an already-installed print agent.
rem It asks Windows for administrator rights, then runs update.ps1, which
rem reads the existing install's settings and reinstalls with them.
setlocal
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
rem In the package this sits at the root, with the script in winagent\;
rem in a repo checkout both are in winagent\.
set "SCRIPT=%~dp0winagent\update.ps1"
if not exist "%SCRIPT%" set "SCRIPT=%~dp0update.ps1"
if not exist "%SCRIPT%" (
  echo Could not find update.ps1 next to this file. Unzip the whole package and try again.
  pause
  exit /b 1
)
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%PS%' -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','%SCRIPT%'"
endlocal
