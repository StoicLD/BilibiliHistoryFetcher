param([switch]$NoPause)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskMutex = New-Object System.Threading.Mutex($false, 'Local\BiliAttention-ManualLauncher')
$taskAcquired = $false
$taskExit = 1
Push-Location $taskRoot
try {
  try { $taskAcquired = $taskMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $taskAcquired = $true }
  if (-not $taskAcquired) {
    Write-Host '已有一个一键日报窗口在执行，请查看原来的窗口。'
    $taskExit = 0
  } else {
    Write-Host 'B 站日报 · 立即生成' -ForegroundColor Cyan
    Write-Host '将同步历史并生成昨天的日报；如有遗漏，会按配置补齐近期报告。'
    Write-Host 'Docker 未启动时会自动启动并等待就绪，请保持本窗口打开。'
    Write-Host ''
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'Run-AttentionJob.ps1') -StartDocker
    $taskExit = $LASTEXITCODE
    $taskInfoText = & $taskPython -m scripts.attention_schedule --config (Join-Path $taskRoot 'config/attention.json')
    if ($LASTEXITCODE -ne 0) { throw '无法读取日报配置。' }
    $taskInfo = $taskInfoText | ConvertFrom-Json
    Write-Host ''
    if ($taskExit -eq 0) {
      Write-Host '执行已结束。以下为最新已发布简报：' -ForegroundColor Green
    } else {
      Write-Host '本次执行失败，旧报告仍然保留。以下显示当前状态：' -ForegroundColor Yellow
    }
    & $taskPython (Join-Path $PSScriptRoot 'attention_reader.py') --output-dir $taskInfo.output_dir
    if ($LASTEXITCODE -ne 0) { $taskExit = 1 }
    Write-Host ''
    Write-Host ('报告目录：' + (Join-Path $taskInfo.output_dir 'reports'))
    Write-Host ('启动日志：' + (Join-Path $taskRoot 'output/attention-host-logs'))
  }
} catch {
  Write-Host ('启动失败：' + $_.Exception.Message) -ForegroundColor Red
  $taskExit = 1
} finally {
  if ($taskAcquired) { $taskMutex.ReleaseMutex() }
  $taskMutex.Dispose()
  Pop-Location
  if (-not $NoPause) { Read-Host '按回车关闭窗口' | Out-Null }
}
exit $taskExit
