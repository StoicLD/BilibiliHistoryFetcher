@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\scripts\Set-AttentionSchedule.ps1" -Mode Enable
exit /b %errorlevel%
