$ErrorActionPreference = 'Stop'
$backendRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$envFile = Join-Path $backendRoot '.env'
if (Test-Path $envFile) {
  $env:PRODUCTLENS_ENV_FILE = $envFile
  Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $name, $value = $line -split '=', 2
    if (-not $name -or $null -eq $value) { return }
    $name = $name.Trim()
    $value = $value.Trim().Trim('"').Trim("'")
    if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
      [Environment]::SetEnvironmentVariable($name, $value, 'Process')
    }
  }
}
Set-Location $backendRoot
# The deployment migration job runs before workers are supervised. A worker
# must never attempt its own schema upgrade while other replicas are live.
python -m app.operations.startup --skip-migrations --require-ready
if ($env:PRODUCTLENS_WORKER_MODE -eq 'dramatiq') {
  dramatiq app.workers.tasks --processes 1 --threads 1
} else {
  python -m app.workers.local
}
