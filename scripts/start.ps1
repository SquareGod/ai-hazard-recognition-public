[CmdletBinding()]
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker was not found. Install Docker Desktop (Windows) or Docker Engine (Linux server) first.'
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is installed but its engine is not running.' }

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created .env from the safe development template. Change all passwords before production use.'
}

& docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose failed. Run: docker compose logs --tail=100' }

Write-Host 'System started: http://localhost:3000'
if (-not $NoBrowser) { Start-Process 'http://localhost:3000' }
