@echo off
setlocal DisableDelayedExpansion
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ImportLogs.ps1" %*
set code=%errorlevel%
if "%~1"=="" pause
exit /b %code%
