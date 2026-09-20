# Refresh the laptop's data\jobs.db from the hosted pipeline (feature 20).
#
# After cutover, GitHub Actions does the polling and the laptop copy goes stale.
# Auto Apply still runs here, so it needs the postings the hosted poll found.
# This downloads the engine file, copies the site's latest triage into it, and
# swaps it in. The old file is kept as data\jobs.db.bak.
#
# The engine keeps descriptions only for open postings that pass, so rejected
# postings show no text here afterwards. The hosted site still has it.
#
#   .\scripts\pull-engine.ps1

$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$jobs   = Join-Path $root 'jobs.py'
$data   = Join-Path $root 'data'
$tmp    = Join-Path $data 'jobs.pulled.db'

$envFile = Join-Path $root '.env.local'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*"?([^"]*)"?\s*$' -and -not [Environment]::GetEnvironmentVariable($Matches[1])) {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
        }
    }
}
if (-not $env:BLOB_READ_WRITE_TOKEN) { Write-Error 'BLOB_READ_WRITE_TOKEN is not set; run: npx vercel env pull'; exit 1 }

$ErrorActionPreference = 'Continue'   # the Vercel CLI writes progress to stderr
npx --yes vercel@59.23.2 blob get engine/jobs.db.gz --access private --output "$tmp.gz" `
    --rw-token $env:BLOB_READ_WRITE_TOKEN --non-interactive 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error 'download failed'; exit 1 }
$ErrorActionPreference = 'Stop'

& $python -c "import gzip, shutil, sys; shutil.copyfileobj(gzip.open(sys.argv[1] + '.gz'), open(sys.argv[1], 'wb'))" $tmp
Remove-Item "$tmp.gz"
& $python $jobs --db $tmp pull
if ($LASTEXITCODE -ne 0) { Write-Error 'pull failed; data\jobs.db left as it was'; exit 1 }

# Swap only once the new file is complete. Stop `jobs.py serve` first: Windows
# will not replace a file another process has open.
$live = Join-Path $data 'jobs.db'
if (Test-Path $live) {
    # Fold the WAL into the file first, so the .bak copy is complete.
    & $python -c "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.execute('PRAGMA journal_mode=DELETE'); c.close()" $live
    if ($LASTEXITCODE -ne 0) { Write-Error 'could not checkpoint data\jobs.db; is jobs.py serve running?'; exit 1 }
}
foreach ($side in '-wal', '-shm') { if (Test-Path "$live$side") { Remove-Item "$live$side" } }
if (Test-Path $live) { Move-Item -Force $live (Join-Path $data 'jobs.db.bak') }
Move-Item $tmp $live
Write-Output 'data\jobs.db refreshed from the hosted engine (previous copy: data\jobs.db.bak)'
