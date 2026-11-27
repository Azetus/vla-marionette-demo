#requires -Version 7.0
param([string]$Config = 'config.local.json')
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (!(Test-Path -LiteralPath $Config)) { throw 'Copy config.example.json to config.local.json and set the client, world and Java paths.' }
    Get-Command uv -ErrorAction Stop | Out-Null
    & ./scripts/setup.ps1
    & ./.venv/Scripts/python.exe -u ./scripts/demo.py $Config
    if ($LASTEXITCODE) { throw "Demo exited with code $LASTEXITCODE. See the run directory printed above." }
} finally { Pop-Location }

