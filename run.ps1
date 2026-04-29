#!/usr/bin/env pwsh
# Convenience launcher for Greezik on Windows.
#
# Activates the local .venv (if present) and runs the bot.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvActivate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    . $venvActivate
} else {
    Write-Host "No .venv detected; using system Python." -ForegroundColor Yellow
}

python -m greezik.main @args
exit $LASTEXITCODE
