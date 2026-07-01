param(
    [string]$AppId = "98693f04-8950-4b10-94d9-ff3dfe7f4aac"
)
$ErrorActionPreference = "Stop"

function Run-Kql([string]$kql, [string]$label) {
    Write-Host "=== $label ==="
    $rows = az monitor app-insights query --app $AppId --analytics-query $kql --query "tables[0].rows" -o json 2>$null
    Write-Host $rows
    Write-Host ""
}

# Distinct files picked up by workers in last 4h
Run-Kql "traces | where timestamp > ago(4h) | where message has 'Worker picked up' | extend item = extract('picked up (\\S+)', 1, message) | summarize distinctFiles = dcount(item), totalPickups = count()" "distinct files vs total pickups (4h)"

# Distinct runs and their pickup counts
Run-Kql "traces | where timestamp > ago(4h) | where message has 'Worker picked up' | extend run = extract('run ([0-9a-f-]+)', 1, message), item = extract('picked up (\\S+)', 1, message) | summarize files = dcount(item), pickups = count() by run | order by pickups desc" "per-run distinct files"

# Chunks uploaded total
Run-Kql "traces | where timestamp > ago(4h) | where message has 'chunks uploaded' | extend n = toint(extract('([0-9]+) chunks uploaded', 1, message)) | summarize completes = count(), totalChunks = sum(n)" "completes and chunks uploaded (4h)"

# Timeouts over time
Run-Kql 'exceptions | where timestamp > ago(4h) | summarize timeouts = count() by bin(timestamp, 10m) | order by timestamp asc' "timeout exceptions over time"
