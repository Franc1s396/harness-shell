$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'M2 verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$labRuntime = Join-Path $workspaceRoot 'tests\ssh_lab\.runtime'
$evidenceRoot = Join-Path $labRuntime 'evidence'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'The locked backend virtual environment is missing'
}

Write-Output '[1/5] M1 regression gate'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-m1.ps1')
if ($LASTEXITCODE -ne 0) { throw 'M1 regression gate failed' }

Write-Output '[2/5] Locked Python dependency check'
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed' }

Write-Output '[3/5] OpenSSH lab script regressions'
foreach ($script in @(
    'test-compose-topology.ps1',
    'test-keygen-arguments.ps1',
    'test-startup-readiness.ps1',
    'test-shell-line-endings.ps1'
)) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $workspaceRoot "tests\ssh_lab\$script")
    if ($LASTEXITCODE -ne 0) { throw "OpenSSH lab regression failed: $script" }
}

Write-Output '[4/5] Real OpenSSH integration'
$labStarted = $false
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
    $labStarted = $true
    New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    $env:HARNESS_RUN_SSH_INTEGRATION = '1'
    $integrationTemp = Join-Path $evidenceRoot 'pytest'
    $sshIntegrationRoot = Join-Path $backendRoot 'tests\ssh_integration'
    & $pythonExe -m pytest -vv --basetemp $integrationTemp -p no:cacheprovider `
        --ignore (Join-Path $sshIntegrationRoot 'test_manual_sftp.py') `
        --ignore (Join-Path $sshIntegrationRoot 'test_pty_and_manual_sftp_isolation.py') `
        $sshIntegrationRoot 2>&1 | Tee-Object -FilePath (Join-Path $evidenceRoot 'integration-output.txt')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH integration tests failed' }
} finally {
    Remove-Item Env:HARNESS_RUN_SSH_INTEGRATION -ErrorAction SilentlyContinue
    if ($labStarted) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    }
}

Write-Output '[5/5] Plaintext schema-v6 evidence and generated-file scan'
& $pythonExe (Join-Path $workspaceRoot 'tests\ssh_lab\check-runtime-evidence.py') $evidenceRoot
if ($LASTEXITCODE -ne 0) { throw 'Runtime database evidence is incomplete' }
$tracked = @(& git.exe -c safe.directory='E:/codeSoftware/code/harness-shell' -C $workspaceRoot ls-files)
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect tracked files' }
$forbiddenTracked = @($tracked | Where-Object {
    $_ -match '(^|/)\.runtime/' -or
    $_ -match '^backend/dist/' -or
    $_ -match '^frontend/src-tauri/binaries/.*\.exe$' -or
    $_ -match '(client_(un)?encrypted|host_ed25519_key)(\.pub)?$' -or
    $_ -match '\.(sqlite3?|db)(-wal|-shm)?$'
})
if ($forbiddenTracked.Count -ne 0) {
    throw "Generated or secret files are tracked: $($forbiddenTracked -join ', ')"
}

Write-Output 'M2 automated gate passed: local Windows checkout and containerized OpenSSH lab against plaintext schema v6 only.'
