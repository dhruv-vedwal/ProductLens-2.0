param(
    [Parameter(Mandatory=$true)][string]$RunId,
    [int]$IntervalSeconds = 10,
    [int]$MaxMinutes = 30
)

$db = Join-Path $PSScriptRoot '..\artifacts\productlens.sqlite3'
$statusFile = Join-Path $PSScriptRoot ("..\artifacts\runs\{0}\run-status.json" -f $RunId)
$deadline = (Get-Date).AddMinutes([Math]::Max(1, $MaxMinutes))
$last = ''

while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path -LiteralPath $db)) { Start-Sleep -Seconds $IntervalSeconds; continue }
    $snapshot = @'
import json, sqlite3, sys
run_id = sys.argv[1]
con = sqlite3.connect(sys.argv[2])
con.row_factory = sqlite3.Row
run = con.execute("select status, stage, error_code, updated_at from demo_runs where id=?", (run_id,)).fetchone()
jobs = con.execute("select stage, status, error_code, completed_at from generation_stage_jobs where run_id=? order by ordinal", (run_id,)).fetchall()
print(json.dumps({"run": dict(run) if run else None, "jobs": [dict(row) for row in jobs]}))
'@
    $current = $snapshot | python - $RunId $db
    if ($current -and $current -ne $last) {
        $last = $current
        $parent = Split-Path -Parent $statusFile
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        $current | Set-Content -LiteralPath $statusFile -Encoding UTF8
    }
    try {
        $state = $current | ConvertFrom-Json
        if ($state.run.status -in @('COMPLETE','FAILED','CANCELLED')) { break }
    } catch { }
    Start-Sleep -Seconds ([Math]::Max(2, $IntervalSeconds))
}
