@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\Start-AttentionReport.ps1"
exit /b %errorlevel%
