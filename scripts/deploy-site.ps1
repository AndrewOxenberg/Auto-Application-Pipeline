# Deploy the hosted site to Vercel (feature 20). Safe to rerun.
#
# Uploads exactly what lands in site\: api\index.py, the jobpipe package and its
# UI, the password middleware, vercel.json, and a requirements file with PyYAML
# alone. public\ holds only robots.txt, so Vercel serves that folder as static
# rather than the source tree. profile\ (home address, resume), data\ and
# config\ are never copied, so they cannot be uploaded by accident, whatever
# .gitignore says.
#
#   .\scripts\deploy-site.ps1            production
#   .\scripts\deploy-site.ps1 -Preview   a preview URL instead
#   .\scripts\deploy-site.ps1 -StageOnly build site\ and stop, to inspect it

param([switch]$Preview, [switch]$StageOnly)
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$site = Join-Path $root 'site'
if (-not (Test-Path (Join-Path $root '.vercel\project.json'))) {
    Write-Error "Not linked yet. From $root run: npx vercel link --yes --project job-pipeline"
    exit 1
}

if (Test-Path $site) { Remove-Item -Recurse -Force $site }
New-Item -ItemType Directory -Path $site | Out-Null
Copy-Item (Join-Path $root 'middleware.ts'), (Join-Path $root 'vercel.json') $site
New-Item -ItemType Directory -Path (Join-Path $site 'api'), (Join-Path $site 'public') | Out-Null
Copy-Item (Join-Path $root 'api\index.py') (Join-Path $site 'api')
Set-Content -Path (Join-Path $site 'public\robots.txt') -Value "User-agent: *`nDisallow: /" -Encoding ascii
Copy-Item -Recurse (Join-Path $root '.vercel') $site
Copy-Item -Recurse (Join-Path $root 'src\jobpipe') (Join-Path $site 'src\jobpipe') -Exclude '__pycache__'
Get-ChildItem -Recurse -Directory -Filter '__pycache__' $site | Remove-Item -Recurse -Force
Set-Content -Path (Join-Path $site 'requirements.txt') -Value 'PyYAML>=6.0' -Encoding ascii

Write-Host 'Uploading:'
Get-ChildItem -Recurse -File $site | Where-Object { $_.FullName -notlike "*\.vercel\*" } |
    ForEach-Object { '  ' + $_.FullName.Substring($site.Length + 1) }

if ($StageOnly) { exit 0 }

# The Vercel CLI writes progress to stderr, which Windows PowerShell 5.1 turns
# into a terminating error under 'Stop'. The exit code is the real signal.
$ErrorActionPreference = 'Continue'
Push-Location $site
try {
    if ($Preview) { npx vercel deploy --yes } else { npx vercel deploy --prod --yes }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
