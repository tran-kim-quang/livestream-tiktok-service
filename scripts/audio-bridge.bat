@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0audio-bridge.ps1" %*
exit /b %errorlevel%
