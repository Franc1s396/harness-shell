$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'M3 Agent verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$tauriManifest = Join-Path $workspaceRoot 'frontend\src-tauri\Cargo.toml'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$cargoExe = Join-Path $env:USERPROFILE '.cargo\bin\cargo.exe'

foreach ($path in @($pythonExe, $cargoExe)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required executable is missing: $path"
    }
}

Push-Location $workspaceRoot
try {
    Write-Output '[1/6] Manual SFTP regression gate'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $PSScriptRoot 'verify-manual-sftp.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Manual SFTP regression gate failed' }
    $packagedSidecar = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'
    if (-not (Test-Path -LiteralPath $packagedSidecar -PathType Leaf)) {
        throw 'Manual SFTP gate did not produce the packaged Sidecar'
    }
    # A child PowerShell cannot export this path back to the parent gate. Own
    # it here so the following Rust all-target tests use the verified binary.
    $env:HARNESS_SIDECAR_EXE = $packagedSidecar

    Write-Output '[2/6] Focused Agent, runtime, and schema tests'
    $agentTemp = Join-Path $env:TEMP "harness-shell-m3-agent-$PID"
    & $pythonExe -m pytest --basetemp $agentTemp -p no:cacheprovider `
        (Join-Path $backendRoot 'tests\agent') `
        (Join-Path $backendRoot 'tests\runtime\test_service_dispatch.py') `
        (Join-Path $backendRoot 'tests\storage\test_database.py') -q
    if ($LASTEXITCODE -ne 0) { throw 'Focused Agent Python tests failed' }

    Write-Output '[3/6] Rust all-target contracts'
    & $cargoExe test --manifest-path $tauriManifest --all-targets
    if ($LASTEXITCODE -ne 0) { throw 'Rust all-target contracts failed' }

    Write-Output '[4/6] Packaged Sidecar build'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
        (Join-Path $backendRoot 'scripts\build_sidecar.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Sidecar package build failed' }

    Write-Output '[5/6] Bound-session OpenSSH Agent integration'
    $labStarted = $false
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
            (Join-Path $PSScriptRoot 'start-ssh-lab.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab startup failed' }
        $labStarted = $true

        $env:HARNESS_RUN_SSH_INTEGRATION = '1'
        $integrationTemp = Join-Path $env:TEMP "harness-shell-m3-agent-integration-$PID"
        & $pythonExe -m pytest --basetemp $integrationTemp -p no:cacheprovider `
            (Join-Path $backendRoot 'tests\ssh_integration\test_agent_command.py') -q
        if ($LASTEXITCODE -ne 0) { throw 'OpenSSH Agent integration failed' }
    }
    finally {
        Remove-Item Env:HARNESS_RUN_SSH_INTEGRATION -ErrorAction SilentlyContinue
        # This lab belongs only to the Agent integration step; never leave its
        # containers alive after success or a test failure.
        if ($labStarted) {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
                (Join-Path $PSScriptRoot 'stop-ssh-lab.ps1')
            if ($LASTEXITCODE -ne 0) { throw 'OpenSSH lab shutdown failed' }
        }
    }

    Write-Output '[6/6] Evidence boundary'
    Write-Output 'M3 Agent automated gate passed: local Windows checkout, fake ChatModels, packaged Sidecar, and containerized OpenSSH lab only.'
}
finally {
    Remove-Item Env:HARNESS_SIDECAR_EXE -ErrorAction SilentlyContinue
    Pop-Location
}
