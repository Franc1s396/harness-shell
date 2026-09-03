$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'Manual SFTP verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$frontendRoot = Join-Path $workspaceRoot 'frontend'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$labRuntime = Join-Path $workspaceRoot 'tests\ssh_lab\.runtime'
$evidenceRoot = Join-Path $labRuntime 'evidence'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'The locked backend virtual environment is missing'
}

Write-Output '[1/5] M2 regression gate'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-m2.ps1')
if ($LASTEXITCODE -ne 0) { throw 'M2 regression gate failed' }

Write-Output '[2/5] Focused Manual SFTP contracts'
$focusedTemp = Join-Path $env:TEMP "harness-shell-manual-sftp-unit-$PID"
& $pythonExe -m pytest --basetemp $focusedTemp -p no:cacheprovider (Join-Path $backendRoot 'tests\manual_sftp') -q
if ($LASTEXITCODE -ne 0) { throw 'Focused Python Manual SFTP tests failed' }
& npm.cmd test --prefix $frontendRoot -- `
    src/api/manual-sftp.test.ts `
    src/features/sftp/browser-file-gateway.test.ts `
    src/features/sftp/browser-sha256.test.ts `
    src/features/sftp/browser-transfer-coordinator.test.ts
if ($LASTEXITCODE -ne 0) { throw 'Focused browser Manual SFTP tests failed' }

Write-Output '[3/5] Static ownership and strict raw-chunk contract'
$gateway = Get-Content -LiteralPath (Join-Path $frontendRoot 'src\features\sftp\browser-file-gateway.ts') -Encoding UTF8 -Raw
$coordinator = Get-Content -LiteralPath (Join-Path $frontendRoot 'src\features\sftp\browser-transfer-coordinator.ts') -Encoding UTF8 -Raw
$client = Get-Content -LiteralPath (Join-Path $frontendRoot 'src\api\manual-sftp.ts') -Encoding UTF8 -Raw
$binaryClient = Get-Content -LiteralPath (Join-Path $frontendRoot 'src\api\http-client.ts') -Encoding UTF8 -Raw
$route = Get-Content -LiteralPath (Join-Path $backendRoot 'src\harness_shell_sidecar\web\routes\manual_sftp.py') -Encoding UTF8 -Raw
foreach ($assertion in @(
    @($gateway, 'SFTP_CHUNK_BYTES = 262_144', 'Browser chunk size'),
    @($gateway, 'showSaveFilePicker', 'Browser download picker'),
    @($coordinator, 'sha256.create()', 'Browser transfer hash'),
    @($binaryClient, 'application/octet-stream', 'Raw media type'),
    @($binaryClient, 'X-Chunk-Offset', 'Strict chunk offset header'),
    @($client, 'putManualSftpUploadChunk', 'Upload raw route client'),
    @($client, 'getManualSftpDownloadChunk', 'Download raw route client'),
    @($route, 'application/octet-stream', 'Backend raw route')
)) {
    if (-not $assertion[0].Contains($assertion[1])) {
        throw "Missing Manual SFTP ownership evidence: $($assertion[2])"
    }
}
foreach ($removedPath in @(
    (Join-Path $frontendRoot 'src-tauri\src\sftp'),
    (Join-Path $backendRoot 'src\harness_shell_sidecar\manual_sftp\local_files.py')
)) {
    if (Test-Path -LiteralPath $removedPath) {
        throw "Removed local-file owner still exists: $removedPath"
    }
}

Write-Output '[4/5] Focused real OpenSSH Manual SFTP integration'
$labStarted = $false
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
    $labStarted = $true
    New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    $env:HARNESS_RUN_SSH_INTEGRATION = '1'
    $env:HARNESS_MANUAL_SFTP_EVENT_EVIDENCE = Join-Path $evidenceRoot 'manual-sftp-events.jsonl'
    $integrationTemp = Join-Path $evidenceRoot 'manual-sftp-pytest'
    & $pythonExe -m pytest -vv --basetemp $integrationTemp -p no:cacheprovider `
        (Join-Path $backendRoot 'tests\ssh_integration\test_manual_sftp.py') `
        (Join-Path $backendRoot 'tests\ssh_integration\test_pty_and_manual_sftp_isolation.py') 2>&1 |
        Tee-Object -FilePath (Join-Path $evidenceRoot 'manual-sftp-integration-output.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Focused OpenSSH Manual SFTP integration failed' }
} finally {
    foreach ($name in @('HARNESS_RUN_SSH_INTEGRATION', 'HARNESS_MANUAL_SFTP_EVENT_EVIDENCE')) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($labStarted) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    }
}

Write-Output '[5/5] Plaintext schema-v6 remote recovery evidence'
& $pythonExe (Join-Path $workspaceRoot 'tests\ssh_lab\check-runtime-evidence.py') $evidenceRoot --manual-sftp
if ($LASTEXITCODE -ne 0) { throw 'Manual SFTP runtime database evidence is incomplete' }

Write-Output 'Manual SFTP automated gate passed: browser-owned local files, strict 256 KiB raw routes, Python-owned remote recovery, and containerized OpenSSH lab only.'
