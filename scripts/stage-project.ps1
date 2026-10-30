param(
    [Parameter(Mandatory = $true)][int]$ProjectId,
    [string]$ApiUrl = "http://localhost:8000",
    [int]$TimeoutMinutes = 45,
    [int]$PollSeconds = 5
)

$ErrorActionPreference = "Stop"
$base = $ApiUrl.TrimEnd('/')

try {
    $live = Invoke-RestMethod -Method Get -Uri "$base/health/live" -TimeoutSec 10
    if ($live.status -ne "ok") { throw "CodeDeck liveness endpoint returned an unexpected response." }
    $ready = Invoke-RestMethod -Method Get -Uri "$base/health/ready" -TimeoutSec 10
    if ($ready.status -ne "ready") { throw "CodeDeck is not ready (database unavailable)." }
} catch {
    throw "CodeDeck API is not ready at $base. Start it and verify Docker, PostgreSQL and kind access. $($_.Exception.Message)"
}

Write-Host "Requesting staging for project $ProjectId..."
try {
    $started = Invoke-RestMethod -Method Post -Uri "$base/api/projects/$ProjectId/deploy/staging" -TimeoutSec 20
} catch {
    throw "Staging request failed: $($_.Exception.Message)"
}
if ($started.status -ne "deploying") {
    throw "Staging did not start: $($started | ConvertTo-Json -Compress)"
}

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds $PollSeconds
    $project = Invoke-RestMethod -Method Get -Uri "$base/api/projects/$ProjectId" -TimeoutSec 15
    Write-Host "Project status: $($project.status)"
    if ($project.status -eq "staging_healthy") {
        Write-Host "Staging rollout and CodeDeck smoke test completed successfully."
        exit 0
    }
    if ($project.status -in @("staging_failed", "failed")) {
        $pipeline = Invoke-RestMethod -Method Get -Uri "$base/api/projects/$ProjectId/pipeline" -TimeoutSec 15
        $step = $pipeline | Where-Object { $_.name -eq "Staging" } | Select-Object -Last 1
        throw "Staging failed. $($step.error) $($step.result)"
    }
} while ((Get-Date) -lt $deadline)

throw "Timed out after $TimeoutMinutes minutes waiting for staging. The operation may still be running; check CodeDeck's pipeline view before retrying."
