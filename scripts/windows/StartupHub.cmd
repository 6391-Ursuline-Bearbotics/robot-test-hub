@echo off
setlocal DisableDelayedExpansion
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0ConfigureStartup.ps1" %*
exit /b %errorlevel%
