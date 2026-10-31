@echo off
rem PAC Sync proof of concept: one double-click. Arguments are passed to start-poc.ps1 (e.g. -Config config\config.mock.yaml).
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-poc.ps1" %*
set "RC=%ERRORLEVEL%"
rem Keep the window open on failure when started by double-click (set PAC_SYNC_NO_PAUSE=1 to disable).
if not "%RC%"=="0" if not defined PAC_SYNC_NO_PAUSE pause
exit /b %RC%
