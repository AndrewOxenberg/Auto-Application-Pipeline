# One-time cutover to the hosted pipeline (feature 20). Also re-uploads the two
# config files whenever you edit them.
#
#   .\scripts\seed-hosted.ps1               seed the site store and the engine file
#   .\scripts\seed-hosted.ps1 -ConfigOnly   re-upload companies.yaml and evidence.yaml
#
# Works on a snapshot of data\jobs.db, which is left exactly as it is. Reads
# TURSO_* and BLOB_READ_WRITE_TOKEN from the environment or from .env.local
# (`npx vercel env pull` writes it). Never uploads profile\facts.yaml.

param([switch]$ConfigOnly)
$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$jobs   = Join-Path $root 'jobs.py'
$work   = Join-Path $root 'site-seed'

# Tokens: environment first, then .env.local.
$envFile = Join-Path $root '.env.local'
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z_]+)\s*=\s*"?([^"]*)"?\s*$' -and -not [Environment]::GetEnvironmentVariable($Matches[1])) {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2])
        }
    }
}
foreach ($name in 'BLOB_READ_WRITE_TOKEN', 'TURSO_DATABASE_URL', 'TURSO_AUTH_TOKEN') {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        Write-Error "$name is not set. Connect the stores to the job-pipeline project, then run: npx vercel env pull"
        exit 1
    }
}

# The Vercel CLI writes progress to stderr; the exit code is the real signal.
function Invoke-Blob([string[]]$BlobArgs) {
    $ErrorActionPreference = 'Continue'
    npx --yes vercel@59.23.2 blob @BlobArgs --rw-token $env:BLOB_READ_WRITE_TOKEN --non-interactive 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "vercel blob $($BlobArgs[0]) failed with exit code $LASTEXITCODE" }
}

function Send-Config {
    $files = @{
        'engine/companies.yaml' = (Join-Path $root 'config\companies.yaml')
        'engine/evidence.yaml'  = (Join-Path $root 'profile\evidence.yaml')
    }
    foreach ($pathname in $files.Keys) {
        if (-not (Test-Path $files[$pathname])) { throw "missing $($files[$pathname])" }
        Invoke-Blob @('put', $files[$pathname], '--pathname', $pathname, '--access', 'private',
                      '--allow-overwrite', '--multipart', 'false')
        Write-Output "uploaded $pathname"
    }
}

if ($ConfigOnly) { Send-Config; exit 0 }

if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Path $work | Out-Null
$engine = Join-Path $work 'jobs.db'

# A backup-API snapshot is consistent even if the local server has the file open.
& $python -c "import sqlite3, sys; s = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" (Join-Path $root 'data\jobs.db') $engine
if ($LASTEXITCODE -ne 0) { throw 'snapshot failed' }
Write-Output 'snapshot of data\jobs.db taken'

& $python $jobs --db $engine push -v
if ($LASTEXITCODE -ne 0) { throw 'push failed' }

& $python $jobs --db $engine strip
& $python -c "import gzip, shutil, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.execute('PRAGMA journal_mode=DELETE'); c.close(); shutil.copyfileobj(open(sys.argv[1], 'rb'), gzip.open(sys.argv[1] + '.gz', 'wb', 6))" $engine
if ($LASTEXITCODE -ne 0) { throw 'compress failed' }
Write-Output ("engine: {0:N1} MB gzipped" -f ((Get-Item "$engine.gz").Length / 1MB))

Invoke-Blob @('put', "$engine.gz", '--pathname', 'engine/jobs.db.gz', '--access', 'private',
              '--allow-overwrite', '--multipart', 'false')
Write-Output 'uploaded engine/jobs.db.gz'
Send-Config

Remove-Item -Recurse -Force $work
Write-Output 'seeded. Next: run the poll workflow once from GitHub, then .\scripts\install-task.ps1 -Uninstall'
