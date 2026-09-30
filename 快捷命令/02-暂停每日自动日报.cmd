@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\Set-AttentionSchedule.ps1" -Mode Disable
exit /b %errorlevel%
