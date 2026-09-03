$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'M3 Agent verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'The locked backend virtual environment is missing'
}

Write-Output '[1/4] Manual SFTP regression gate'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-manual-sftp.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Manual SFTP regression gate failed' }

Write-Output '[2/4] Focused Agent, runtime, and schema tests'
$agentTemp = Join-Path $env:TEMP "harness-shell-m3-agent-$PID"
& $pythonExe -m pytest --basetemp $agentTemp -p no:cacheprovider `
    (Join-Path $backendRoot 'tests\agent') `
    (Join-Path $backendRoot 'tests\web\test_agent_routes.py') `
    (Join-Path $backendRoot 'tests\runtime\test_dispatcher.py') `
    (Join-Path $backendRoot 'tests\storage\test_database.py') -q
if ($LASTEXITCODE -ne 0) { throw 'Focused Agent Python tests failed' }

Write-Output '[3/4] Python CredentialRepository ownership'
$handlers = Get-Content -LiteralPath (Join-Path $backendRoot 'src\harness_shell_sidecar\agent\handlers.py') -Encoding UTF8 -Raw
$resources = Get-Content -LiteralPath (Join-Path $backendRoot 'src\harness_shell_sidecar\runtime\resources.py') -Encoding UTF8 -Raw
foreach ($source in @($handlers, $resources)) {
    if (-not $source.Contains('CredentialRepository')) {
        throw 'Provider key resolution is not wired to Python CredentialRepository'
    }
}
if ($handlers.Contains('api_key_b64')) {
    throw 'Agent handler still accepts a transported Provider key payload'
}

Write-Output '[4/4] Bound-session OpenSSH Agent integration'
$labStarted = $false
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
    $labStarted = $true
    $env:HARNESS_RUN_SSH_INTEGRATION = '1'
    $integrationTemp = Join-Path $env:TEMP "harness-shell-m3-agent-integration-$PID"
    & $pythonExe -m pytest --basetemp $integrationTemp -p no:cacheprovider `
        (Join-Path $backendRoot 'tests\ssh_integration\test_agent_command.py') -q
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH Agent integration failed' }
} finally {
    Remove-Item Env:HARNESS_RUN_SSH_INTEGRATION -ErrorAction SilentlyContinue
    if ($labStarted) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    }
}

Write-Output 'M3 Agent automated gate passed: Python CredentialRepository, fake ChatModels, and containerized OpenSSH lab only.'
