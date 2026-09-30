param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('Enable', 'Disable')]
  [string]$Mode,
  [switch]$NoPause
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskConfigPath = Join-Path $taskRoot 'config/attention.json'
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskApplyScript = Join-Path $PSScriptRoot 'Apply-AttentionConfig.ps1'
$taskMutex = New-Object System.Threading.Mutex($false, 'Local\BiliAttention-ScheduleControl')
$taskAcquired = $false
$taskExit = 1

function Set-AttentionConfigBytes {
  param([byte[]]$Bytes)
  $taskTemporary = $taskConfigPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
  $taskBackup = $taskTemporary + '.bak'
  try {
    [IO.File]::WriteAllBytes($taskTemporary, $Bytes)
    [IO.File]::Replace($taskTemporary, $taskConfigPath, $taskBackup)
  } finally {
    if (Test-Path -LiteralPath $taskTemporary) { Remove-Item -LiteralPath $taskTemporary -Force }
    if (Test-Path -LiteralPath $taskBackup) { Remove-Item -LiteralPath $taskBackup -Force }
  }
}

Push-Location $taskRoot
try {
  try { $taskAcquired = $taskMutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $taskAcquired = $true }
  if (-not $taskAcquired) { throw '另一个窗口正在修改自动日报设置，请稍后重试。' }
  if (-not (Test-Path -LiteralPath $taskPython)) { throw '项目 Python 环境不存在，请先运行 uv sync。' }
  & $taskPython -m scripts.attention_job --config $taskConfigPath --check-config | Out-Null
  if ($LASTEXITCODE -ne 0) { throw '日报配置无效，未修改自动任务。' }

  $taskOriginalBytes = [IO.File]::ReadAllBytes($taskConfigPath)
  $taskConfig = [IO.File]::ReadAllText($taskConfigPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
  if (-not $taskConfig.PSObject.Properties['schedule']) { $taskConfig | Add-Member NoteProperty schedule ([pscustomobject]@{}) }
  if (-not $taskConfig.schedule.PSObject.Properties['enabled']) { $taskConfig.schedule | Add-Member NoteProperty enabled $true }
  $taskConfig.schedule.enabled = ($Mode -eq 'Enable')
  $taskNewBytes = [Text.Encoding]::UTF8.GetBytes(($taskConfig | ConvertTo-Json -Depth 32) + [Environment]::NewLine)
  Set-AttentionConfigBytes $taskNewBytes
  try {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $taskApplyScript -Config $taskConfigPath
    if ($LASTEXITCODE -ne 0) { throw 'Windows 计划任务未能更新。' }
    $taskState = Get-ScheduledTask -TaskName 'BiliAttention-DailyReport' -ErrorAction Stop
    if ([bool]$taskState.Settings.Enabled -ne ($Mode -eq 'Enable')) { throw 'Windows 计划任务状态与配置不一致。' }
  } catch {
    Set-AttentionConfigBytes $taskOriginalBytes
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $taskApplyScript -Config $taskConfigPath
    if ($LASTEXITCODE -ne 0) {
      throw '操作失败。JSON 配置已还原，但计划任务恢复失败，请查看上方错误信息。'
    }
    throw '操作失败，已恢复之前的配置与计划任务，请查看上方错误信息。'
  }

  Write-Host ''
  if ($Mode -eq 'Disable') {
    Write-Host '已暂停每日自动生成日报。' -ForegroundColor Yellow
    Write-Host '手动生成和微信 /bili 查询仍可使用；已经开始的日报任务会继续完成。'
  } else {
    Write-Host '已启用每日自动生成日报。' -ForegroundColor Green
    $taskNextRun = (Get-ScheduledTaskInfo -TaskName 'BiliAttention-DailyReport').NextRunTime
    Write-Host ('下次计划执行：' + $taskNextRun.ToString('yyyy-MM-dd HH:mm:ss'))
    Write-Host '按配置文件中的每日时间执行。若要现在补跑，请双击“01-生成昨日日报”。'
  }
  $taskExit = 0
} catch {
  Write-Host ('设置失败：' + $_.Exception.Message) -ForegroundColor Red
} finally {
  if ($taskAcquired) { $taskMutex.ReleaseMutex() }
  $taskMutex.Dispose()
  Pop-Location
  if (-not $NoPause) { Read-Host '按回车关闭窗口' | Out-Null }
}
exit $taskExit
