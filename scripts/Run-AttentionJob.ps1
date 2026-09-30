param(
  [string]$Config = '',
  [string]$Credentials = '',
  [string]$Date = '',
  [switch]$Scheduled,
  [switch]$StartDocker
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$taskHostLogDir = Join-Path $taskRoot 'output/attention-host-logs'
[IO.Directory]::CreateDirectory($taskHostLogDir) | Out-Null
$taskHostLog = Join-Path $taskHostLogDir ('host-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.log')
Start-Transcript -LiteralPath $taskHostLog -Force | Out-Null
if (-not $Config) { $Config = Join-Path $taskRoot 'config/attention.json' }
if (-not $Credentials) { $Credentials = Join-Path $taskRoot 'secrets/bilibili.env' }
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project Python runtime missing; run uv sync first.' }
Push-Location $taskRoot
try {
  $taskInfoText = & $taskPython -m scripts.attention_schedule --config $Config
  if ($LASTEXITCODE -ne 0) { throw 'Invalid attention configuration.' }
  $taskInfo = $taskInfoText | ConvertFrom-Json
  if ($Scheduled -and -not $taskInfo.due) { exit 0 }
  $env:ATTENTION_HOST_OUTPUT = $taskInfo.output_dir
  $env:ATTENTION_CONFIG_FILE = (Resolve-Path -LiteralPath $Config).Path
  $env:ATTENTION_CREDENTIALS_FILE = [IO.Path]::GetFullPath($Credentials)
  [IO.Directory]::CreateDirectory($taskInfo.output_dir) | Out-Null
  $taskDocker = (Get-Command docker -ErrorAction SilentlyContinue).Source
  if (-not $taskDocker) { $taskDocker = 'C:/Program Files/Docker/Docker/resources/bin/docker.exe' }
  try {
    . (Join-Path $PSScriptRoot 'AttentionDocker.ps1')
    Wait-AttentionDocker -DockerCommand $taskDocker -StartIfNeeded:$StartDocker
    if (-not (Test-Path -LiteralPath $env:ATTENTION_CREDENTIALS_FILE -PathType Leaf)) { throw 'Bilibili credential file is missing.' }
    $taskArguments = @('compose', '-f', (Join-Path $taskRoot 'docker-compose.attention-job.yml'), 'run', '--rm', '--no-deps', '-T', 'job', '--config', '/app/config/attention.json', '--credentials', '/run/secrets/bilibili.env')
    if ($Date) { $taskArguments += @('--date', $Date) }
    $taskStateFile = Join-Path $taskInfo.output_dir 'run-state.json'
    $taskPreviousWrite = if (Test-Path -LiteralPath $taskStateFile) { (Get-Item -LiteralPath $taskStateFile).LastWriteTimeUtc } else { $null }
    & $taskDocker @taskArguments
    $taskExit = $LASTEXITCODE
    if ($taskExit -ne 0) {
      $taskCurrentWrite = if (Test-Path -LiteralPath $taskStateFile) { (Get-Item -LiteralPath $taskStateFile).LastWriteTimeUtc } else { $null }
      if ($taskPreviousWrite -eq $taskCurrentWrite) { throw 'Daily report container could not complete startup.' }
      exit $taskExit
    }
    exit 0
  } catch {
    # Only write a host failure when no running job holds the same output lock.
    $env:ATTENTION_HOST_FAILURE = $_.Exception.Message
    & $taskPython -m scripts.attention_schedule --config $Config --record-failure
    Write-Error $env:ATTENTION_HOST_FAILURE
    exit 1
  }
} finally {
  Pop-Location
  Stop-Transcript | Out-Null
}
