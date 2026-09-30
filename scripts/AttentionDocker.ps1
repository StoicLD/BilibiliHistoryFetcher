function Test-AttentionDockerReady {
  param([string]$DockerCommand)
  $taskProbe = New-Object System.Diagnostics.Process
  $taskProbe.StartInfo.FileName = $DockerCommand
  $taskProbe.StartInfo.Arguments = 'info --format {{.ServerVersion}}'
  $taskProbe.StartInfo.UseShellExecute = $false
  $taskProbe.StartInfo.CreateNoWindow = $true
  $taskProbe.StartInfo.RedirectStandardOutput = $true
  $taskProbe.StartInfo.RedirectStandardError = $true
  try {
    if (-not $taskProbe.Start()) { return $false }
    $taskProbeOutput = $taskProbe.StandardOutput.ReadToEndAsync()
    $taskProbeError = $taskProbe.StandardError.ReadToEndAsync()
    if (-not $taskProbe.WaitForExit(10000)) {
      $taskProbe.Kill()
      $taskProbe.WaitForExit()
      return $false
    }
    return $taskProbe.ExitCode -eq 0
  } catch {
    return $false
  } finally {
    $taskProbe.Dispose()
  }
}

function Wait-AttentionDocker {
  param(
    [string]$DockerCommand,
    [switch]$StartIfNeeded,
    [int]$StartupTimeoutSeconds = 180
  )
  if (-not (Test-Path -LiteralPath $DockerCommand -PathType Leaf)) {
    throw 'Docker CLI is not installed. Install Docker Desktop first.'
  }
  if (Test-AttentionDockerReady $DockerCommand) { return }
  if (-not $StartIfNeeded) { throw 'Docker engine is not ready. Start Docker Desktop.' }
  $taskDesktopCandidates = @(
    (Join-Path $env:ProgramFiles 'Docker/Docker/Docker Desktop.exe'),
    (Join-Path $env:LOCALAPPDATA 'Docker/Docker Desktop.exe')
  )
  $taskDesktop = $taskDesktopCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
  if (-not $taskDesktop) { throw 'Docker Desktop application could not be found.' }
  if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $taskDesktop -WindowStyle Hidden | Out-Null
  }
  Write-Host 'Starting Docker Desktop; waiting for the engine (up to 3 minutes)...'
  $taskDeadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
  while ([DateTime]::UtcNow -lt $taskDeadline) {
    if (Test-AttentionDockerReady $DockerCommand) { return }
    Start-Sleep -Seconds 3
  }
  throw 'Docker did not become ready. Open Docker Desktop, check WSL, then try again.'
}
