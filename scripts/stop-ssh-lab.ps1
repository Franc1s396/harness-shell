$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$labRoot = Join-Path $workspaceRoot 'tests\ssh_lab'
Push-Location $labRoot
try {
    & docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 down --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw 'SSH lab shutdown failed' }
} finally {
    Pop-Location
}
