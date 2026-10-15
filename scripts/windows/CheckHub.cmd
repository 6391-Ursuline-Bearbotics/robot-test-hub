@echo off
setlocal DisableDelayedExpansion
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0SetupHub.ps1" -Action Check %*
exit /b %errorlevel%
