# Poll every source, dedup, re-run Tier-1, then report source health.
# Registered as a Scheduled Task by install-task.ps1. Safe to run by hand.
#
# Exit codes: 0 clean, 1 ingest failed, 2 ingest fine but sources are alerting.

$ErrorActionPreference = 'Continue'

$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$jobs   = Join-Path $root 'jobs.py'
$logDir = Join-Path $root 'data\logs'

if (-not (Test-Path $python)) {
    Write-Error "No venv at $python. Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("poll-" + (Get-Date -Format 'yyyy-MM-dd') + ".log")
$errFile = Join-Path $env:TEMP ("jobpipe-poll-err-" + $PID + ".txt")

function Write-Log($text) { $text | Add-Content -Path $log -Encoding utf8 }

# Run a jobs.py subcommand, logging BOTH streams. Capturing stderr is the point:
# a crash writes only to stderr, so without this a failed run logs nothing at
# all and the scheduled task looks like it merely found alerts.
function Invoke-Jobs {
    param([string[]]$JobArgs)

    if (Test-Path $errFile) { Remove-Item $errFile -Force }
    & $python $jobs @JobArgs 2>$errFile | ForEach-Object { Write-Log $_ }
    $code = $LASTEXITCODE

    if (Test-Path $errFile) {
        $stderr = Get-Content $errFile -ErrorAction SilentlyContinue
        if ($stderr) {
            Write-Log "--- stderr ---"
            $stderr | ForEach-Object { Write-Log $_ }
            Write-Log "--- end stderr ---"
        }
        Remove-Item $errFile -Force -ErrorAction SilentlyContinue
    }
    return $code
}

Write-Log ""
Write-Log ("=== " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " poll start ===")

$started    = Get-Date
$ingestCode = Invoke-Jobs @('ingest', '--workers', '12')
$elapsed    = [int]((Get-Date) - $started).TotalSeconds
Write-Log "ingest exit $ingestCode in ${elapsed}s"

if ($ingestCode -ne 0) {
    Write-Log "POLL FAILED, see stderr above"
    exit 1
}

# Score whatever just cleared Tier-1, so a posting found at 04:00 is ranked by
# the time anyone looks. Exits 0 and does nothing when no API key is set, so a
# machine without one still polls cleanly.
Invoke-Jobs @('score', '--new', '--yes') | Out-Null

# Non-zero here means at least one source has been failing or returning an
# empty payload for two consecutive runs. The UI shows the same alert.
$healthCode = Invoke-Jobs @('sources', '--alerts')

# Keep two weeks of logs. Nobody reads month-old poll output.
Get-ChildItem -Path $logDir -Filter 'poll-*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

if ($healthCode -ne 0) {
    Write-Log "poll ok, sources alerting (exit $healthCode)"
    exit 2
}

Write-Log "poll ok, all sources healthy"
exit 0
