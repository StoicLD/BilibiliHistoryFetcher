param([string]$Config = '', [switch]$Uninstall, [switch]$Preview)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskName = 'BiliAttention-DailyReport'
if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  exit 0
}
if (-not $Config) { $Config = Join-Path $taskRoot 'config/attention.json' }
$Config = (Resolve-Path -LiteralPath $Config).Path
Push-Location $taskRoot
try {
  $taskText = & (Join-Path $taskRoot '.venv/Scripts/python.exe') -m scripts.attention_schedule --config $Config
  if ($LASTEXITCODE -ne 0) { throw 'Invalid attention configuration.' }
  $taskInfo = $taskText | ConvertFrom-Json
  if ($taskInfo.start_boundary -is [datetime]) { $taskInfo.start_boundary = $taskInfo.start_boundary.ToString('o') }
  $taskSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  $taskScript = Join-Path $PSScriptRoot 'Run-AttentionJob.ps1'
  $taskArgs = [Security.SecurityElement]::Escape('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskScript + '" -Config "' + $Config + '" -Scheduled')
  $taskEnabled = $taskInfo.schedule.enabled.ToString().ToLowerInvariant()
  $taskLimit = [int]$taskInfo.job_timeout_minutes + 5
  $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Bilibili attention report; Docker one-shot job; configured timezone: $($taskInfo.timezone).</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger><StartBoundary>$($taskInfo.start_boundary)</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <LogonTrigger><Enabled>true</Enabled><Delay>PT2M</Delay><UserId>$taskSid</UserId></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$taskSid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>$taskEnabled</Enabled><ExecutionTimeLimit>PT${taskLimit}M</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>$taskArgs</Arguments><WorkingDirectory>$([Security.SecurityElement]::Escape($taskRoot))</WorkingDirectory></Exec></Actions>
</Task>
"@
  if ($Preview) { $taskXml; exit 0 }
  Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Select-Object TaskName, State
} finally {
  Pop-Location
}
