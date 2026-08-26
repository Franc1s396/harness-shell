$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'M2 verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$frontendRoot = Join-Path $workspaceRoot 'frontend'
$tauriRoot = Join-Path $frontendRoot 'src-tauri'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$cargoExe = Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'
$labRuntime = Join-Path $workspaceRoot 'tests\ssh_lab\.runtime'
$evidenceRoot = Join-Path $labRuntime 'evidence'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw 'The locked backend virtual environment is missing'
}
if (-not (Test-Path -LiteralPath $cargoExe -PathType Leaf)) {
    throw 'cargo.exe was not found in the current user Cargo bin directory'
}

Write-Output '[1/8] M1 regression gate'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'verify-m1.ps1')
if ($LASTEXITCODE -ne 0) { throw 'M1 regression gate failed' }

Write-Output '[2/8] Python locked dependency check'
& $pythonExe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency check failed' }
$installed = @(& $pythonExe -m pip list --format=freeze)
$locked = @(Get-Content -Encoding UTF8 -LiteralPath (Join-Path $backendRoot 'build-requirements.lock') | Where-Object { $_ -and -not $_.StartsWith('#') })
foreach ($package in $locked) {
    if ($installed -notcontains $package) { throw "Locked Python package is missing: $package" }
}

Write-Output '[3/8] Python unit and contract tests'
$env:HARNESS_RUN_SSH_INTEGRATION = $null
$unitTemp = Join-Path $env:TEMP "harness-shell-m2-unit-$PID"
& $pythonExe -m pytest --basetemp $unitTemp -p no:cacheprovider $backendRoot
if ($LASTEXITCODE -ne 0) { throw 'Python unit and contract tests failed' }

Write-Output '[4/8] Sidecar package build'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backendRoot 'scripts\build_sidecar.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Sidecar package build failed' }
$env:HARNESS_SIDECAR_EXE = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'

Write-Output '[5/8] Rust all-target tests'
& $cargoExe test --manifest-path (Join-Path $tauriRoot 'Cargo.toml') --all-targets
if ($LASTEXITCODE -ne 0) { throw 'Rust all-target tests failed' }

Write-Output '[6/8] Web tests and production build'
& npm.cmd ci --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
& npm.cmd test --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Web tests failed' }
& npm.cmd run build --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Web production build failed' }

Write-Output '[7/8] OpenSSH lab integration tests'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $workspaceRoot 'tests\ssh_lab\test-compose-topology.ps1')
if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab Compose topology regression failed' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $workspaceRoot 'tests\ssh_lab\test-keygen-arguments.ps1')
if ($LASTEXITCODE -ne 0) { throw 'OpenSSH key generation argument regression failed' }
$labStarted = $false
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
    $labStarted = $true
    New-Item -ItemType Directory -Path $evidenceRoot -Force | Out-Null
    $env:HARNESS_RUN_SSH_INTEGRATION = '1'
    $integrationTemp = Join-Path $evidenceRoot 'pytest'
    & $pythonExe -m pytest -vv --basetemp $integrationTemp -p no:cacheprovider (Join-Path $backendRoot 'tests\ssh_integration') 2>&1 |
        Tee-Object -FilePath (Join-Path $evidenceRoot 'integration-output.txt')
    if ($LASTEXITCODE -ne 0) { throw 'OpenSSH integration tests failed' }
} finally {
    $env:HARNESS_RUN_SSH_INTEGRATION = $null
    if ($labStarted) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
    }
}

Write-Output '[8/8] Secret-marker and generated-file scan'
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
$vaultEvidencePath = Join-Path $evidenceRoot 'vault.sqlite3'
$env:HARNESS_M2_VAULT_EVIDENCE_DB = $vaultEvidencePath
$env:HARNESS_M2_JUMP_PASSWORD = [string]$runtimeSecrets.jump_password
$env:HARNESS_M2_TARGET_PASSWORD = [string]$runtimeSecrets.target_password
$env:HARNESS_M2_KEY_PASSPHRASE = [string]$runtimeSecrets.private_key_passphrase
$env:HARNESS_M2_PLAIN_KEY_PATH = Join-Path $labRuntime 'client_unencrypted_ed25519'
$env:HARNESS_M2_ENCRYPTED_KEY_PATH = Join-Path $labRuntime 'client_encrypted_ed25519'
try {
    & $cargoExe test --manifest-path (Join-Path $tauriRoot 'Cargo.toml') --test vault_contract writes_runtime_lab_vault_evidence_when_requested -- --ignored --exact
    if ($LASTEXITCODE -ne 0) { throw 'Runtime Vault evidence generation failed' }
} finally {
    foreach ($name in @(
        'HARNESS_M2_VAULT_EVIDENCE_DB',
        'HARNESS_M2_JUMP_PASSWORD',
        'HARNESS_M2_TARGET_PASSWORD',
        'HARNESS_M2_KEY_PASSPHRASE',
        'HARNESS_M2_PLAIN_KEY_PATH',
        'HARNESS_M2_ENCRYPTED_KEY_PATH'
    )) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
$integrationOutput = Join-Path $evidenceRoot 'integration-output.txt'
if (-not (Test-Path -LiteralPath $integrationOutput -PathType Leaf)) {
    throw 'Captured OpenSSH integration output is missing'
}
$runtimeDatabases = @(Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -ErrorAction SilentlyContinue | Where-Object {
    $_.Extension -in @('.db', '.sqlite', '.sqlite3')
})
if ($runtimeDatabases.Count -eq 0) {
    throw 'OpenSSH integration runtime database evidence is missing'
}
foreach ($file in Get-ChildItem -LiteralPath $evidenceRoot -File -Recurse -ErrorAction SilentlyContinue) {
    $content = $utf8.GetString([IO.File]::ReadAllBytes($file.FullName))
    foreach ($marker in $markers) {
        if ($content.Contains($marker)) {
            throw "Secret marker leaked into evidence file: $($file.FullName)"
        }
    }
}

$databaseAscii = ($runtimeDatabases | ForEach-Object {
    [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($_.FullName))
}) -join "`n"
foreach ($requiredStore in @(
    'audit_entries',
    'trace_spans',
    'artifact_metadata',
    'encrypted_records',
    'vault_meta',
    'vault_secrets',
    'vault_keys'
)) {
    if (-not $databaseAscii.Contains($requiredStore)) {
        throw "Required runtime evidence store is missing: $requiredStore"
    }
}
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

Write-Output 'M2 automated gate passed: local Windows checkout plus containerized OpenSSH lab only.'
