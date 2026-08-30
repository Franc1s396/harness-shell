$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'Manual SFTP verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$frontendRoot = Join-Path $workspaceRoot 'frontend'
$tauriRoot = Join-Path $frontendRoot 'src-tauri'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
$cargoExe = Join-Path $cargoBin 'cargo.exe'
$labRuntime = Join-Path $workspaceRoot 'tests\ssh_lab\.runtime'
$evidenceRoot = Join-Path $labRuntime 'evidence'
$labStarted = $false

foreach ($path in @($pythonExe, $cargoExe)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required executable is missing: $path"
    }
}
foreach ($command in @('npm.cmd', 'docker.exe', 'docker-compose.exe', 'ssh-keygen.exe')) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found on PATH"
    }
}
$pythonVersion = & $pythonExe -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.13') {
    throw "Manual SFTP verification requires Python 3.12.13, found $pythonVersion"
}
& docker.exe version --format '{{.Server.Version}}' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop engine is unavailable' }
$composeVersion = (& docker-compose.exe version --short).Trim()
if (
    $LASTEXITCODE -ne 0 -or
    $composeVersion -notmatch '^v?(\d+)\.' -or
    [int]$Matches[1] -lt 2
) {
    throw 'Docker Compose v2 or newer is unavailable'
}

try {
    Write-Output '[1/7] M2 regression gate'
    $m2Output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-m2.ps1'))
    if ($LASTEXITCODE -ne 0) { throw 'M2 regression gate failed' }
    $m2Output |
        Where-Object { $_ -ne 'M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.' } |
        Write-Output

    Write-Output '[2/7] Focused Python manual SFTP contracts'
    $env:HARNESS_RUN_SSH_INTEGRATION = $null
    $focusedTemp = Join-Path $env:TEMP "harness-shell-manual-sftp-unit-$PID"
    & $pythonExe -m pytest --basetemp $focusedTemp -p no:cacheprovider (Join-Path $backendRoot 'tests\manual_sftp') -v
    if ($LASTEXITCODE -ne 0) { throw 'Focused Python manual SFTP tests failed' }

    Write-Output '[3/7] Packaged Sidecar and Rust all-target contracts'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backendRoot 'scripts\build_sidecar.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Sidecar package build failed' }
    $env:HARNESS_SIDECAR_EXE = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'
    if (-not (Test-Path -LiteralPath $env:HARNESS_SIDECAR_EXE -PathType Leaf)) {
        throw 'Packaged Sidecar output is missing'
    }
    & $cargoExe test --manifest-path (Join-Path $tauriRoot 'Cargo.toml') --all-targets
    if ($LASTEXITCODE -ne 0) { throw 'Rust all-target contracts failed' }

    Write-Output '[4/7] Frontend tests and production build'
    & npm.cmd test --prefix $frontendRoot
    if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed' }
    & npm.cmd run build --prefix $frontendRoot
    if ($LASTEXITCODE -ne 0) { throw 'Frontend production build failed' }

    Write-Output '[5/7] Focused real OpenSSH manual SFTP integration'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
    $labStarted = $true
    New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    $env:HARNESS_RUN_SSH_INTEGRATION = '1'
    $env:HARNESS_MANUAL_SFTP_EVENT_EVIDENCE = Join-Path $evidenceRoot 'manual-sftp-events.jsonl'
    $integrationTemp = Join-Path $evidenceRoot 'pytest'
    & $pythonExe -m pytest -vv --basetemp $integrationTemp -p no:cacheprovider `
        (Join-Path $backendRoot 'tests\ssh_integration\test_manual_sftp.py') `
        (Join-Path $backendRoot 'tests\ssh_integration\test_pty_and_manual_sftp_isolation.py') 2>&1 |
        Tee-Object -FilePath (Join-Path $evidenceRoot 'manual-sftp-integration-output.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Focused OpenSSH manual SFTP integration failed' }

    Write-Output '[6/7] Container log capture and deterministic cleanup'
    $labRoot = Join-Path $workspaceRoot 'tests\ssh_lab'
    Push-Location $labRoot
    try {
        $containerLogs = @(& docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 logs --no-color 2>&1)
        if ($LASTEXITCODE -ne 0) { throw 'Unable to capture OpenSSH lab logs' }
        [IO.File]::WriteAllLines(
            (Join-Path $evidenceRoot 'container.log'),
            [string[]]$containerLogs,
            [Text.UTF8Encoding]::new($false)
        )
    } finally {
        Pop-Location
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab shutdown failed' }
    $labStarted = $false
    $env:HARNESS_RUN_SSH_INTEGRATION = $null
    $env:HARNESS_MANUAL_SFTP_EVENT_EVIDENCE = $null

    Write-Output '[7/7] Credential, local-path, file-content, and evidence scan'
    $secretsPath = Join-Path $labRuntime 'secrets.json'
    if (-not (Test-Path -LiteralPath $secretsPath -PathType Leaf)) {
        throw 'SSH lab runtime secrets are unavailable for the evidence scan'
    }
    $runtimeSecrets = Get-Content -Encoding UTF8 -LiteralPath $secretsPath | ConvertFrom-Json
    $utf8 = [Text.Encoding]::UTF8
    $markers = [Collections.Generic.List[string]]::new()
    foreach ($secret in @(
        $runtimeSecrets.jump_password,
        $runtimeSecrets.target_password,
        $runtimeSecrets.private_key_passphrase
    )) {
        $markers.Add([string]$secret)
        $markers.Add([Convert]::ToBase64String($utf8.GetBytes([string]$secret)))
    }
    foreach ($privateKeyName in @('client_unencrypted_ed25519', 'client_encrypted_ed25519')) {
        $privateKeyPath = Join-Path $labRuntime $privateKeyName
        if (-not (Test-Path -LiteralPath $privateKeyPath -PathType Leaf)) {
            throw "SSH lab private key is unavailable for the evidence scan: $privateKeyName"
        }
        $privateKeyBytes = [IO.File]::ReadAllBytes($privateKeyPath)
        $markers.Add([Convert]::ToBase64String($privateKeyBytes))
        foreach ($line in Get-Content -Encoding UTF8 -LiteralPath $privateKeyPath | Where-Object { $_.Length -ge 24 }) {
            $markers.Add($line)
        }
    }
    foreach ($marker in @(
        'C:\secret-marker\payload.bin',
        'C:\recovery\payload.bin',
        'manual-sftp-payload',
        'external-client-change'
    )) {
        $markers.Add($marker)
        $markers.Add([Convert]::ToBase64String($utf8.GetBytes($marker)))
    }
    $evidenceFiles = @(Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -ErrorAction SilentlyContinue)
    if ($evidenceFiles.Count -eq 0) { throw 'Manual SFTP evidence files are missing' }
    foreach ($file in $evidenceFiles) {
        $content = $utf8.GetString([IO.File]::ReadAllBytes($file.FullName))
        foreach ($marker in $markers) {
            if ($content.Contains($marker)) {
                throw "Sensitive marker leaked into manual SFTP evidence: $($file.FullName)"
            }
        }
    }
    & $pythonExe (Join-Path $workspaceRoot 'tests\ssh_lab\check-runtime-evidence.py') $evidenceRoot --manual-sftp
    if ($LASTEXITCODE -ne 0) { throw 'Manual SFTP runtime database evidence is incomplete' }

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
} finally {
    foreach ($name in @(
        'HARNESS_RUN_SSH_INTEGRATION',
        'HARNESS_MANUAL_SFTP_EVENT_EVIDENCE',
        'HARNESS_SIDECAR_EXE'
    )) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
    if ($labStarted) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    }
}

Write-Output 'Manual SFTP automated gate passed: local Windows checkout plus containerized OpenSSH lab only.'
