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
# Migrations are a one-off deployment job. Replicas only verify that the
# already-migrated dependencies are healthy, preventing concurrent API
# processes from racing schema upgrades during a rollout.
python -m app.operations.startup --skip-migrations --require-ready
uvicorn app.api.main:app --host 0.0.0.0 --port 8000
