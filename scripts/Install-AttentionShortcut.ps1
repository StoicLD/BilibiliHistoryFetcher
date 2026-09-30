param([string]$Desktop = [Environment]::GetFolderPath('Desktop'))
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskScript = Join-Path $PSScriptRoot 'Start-AttentionReport.ps1'
$taskShell = New-Object -ComObject WScript.Shell
$taskLinkPath = Join-Path $Desktop 'B站日报-立即生成.lnk'
$taskShortcut = $taskShell.CreateShortcut($taskLinkPath)
$taskShortcut.TargetPath = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$taskShortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $taskScript + '"'
$taskShortcut.WorkingDirectory = $taskRoot
$taskShortcut.Description = '同步 B 站历史并生成本地日报，自动启动 Docker，完成后显示简报。'
$taskShortcut.IconLocation = (Join-Path $env:SystemRoot 'System32/shell32.dll') + ',239'
$taskShortcut.WindowStyle = 1
$taskShortcut.Save()
Write-Output $taskLinkPath
