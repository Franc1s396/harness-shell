$ErrorActionPreference = 'Stop'

if (-not $IsWindows -and $env:OS -ne 'Windows_NT') {
    throw 'M1 verification requires Windows'
}

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$backendRoot = Join-Path $workspaceRoot 'backend'
$frontendRoot = Join-Path $workspaceRoot 'frontend'
$tauriRoot = Join-Path $frontendRoot 'src-tauri'
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$cargoBin = Join-Path $env:USERPROFILE '.cargo\bin'
$cargoExe = Join-Path $cargoBin 'cargo.exe'
$sidecarExe = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'

foreach ($path in @($pythonExe, $cargoExe)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required executable is missing: $path"
    }
}
if ($null -eq (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'npm.cmd was not found on PATH'
}
$pythonVersion = & $pythonExe -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.13') {
    throw "M1 requires Python 3.12.13, found $pythonVersion"
}
$rustHost = & (Join-Path $cargoBin 'rustc.exe') -vV | Where-Object { $_ -match '^host:' }
if ($LASTEXITCODE -ne 0 -or $rustHost -ne 'host: x86_64-pc-windows-msvc') {
    throw "M1 requires the x86_64-pc-windows-msvc rustc host, found $rustHost"
}

Write-Output '[1/6] Python tests'
$pytestTemp = Join-Path $env:TEMP "harness-shell-m1-pytest-$PID"
& $pythonExe -m pytest --basetemp $pytestTemp -p no:cacheprovider $backendRoot
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }

Write-Output '[2/6] Packaged Backend build'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backendRoot 'scripts\build_sidecar.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Packaged Backend build failed' }
if (-not (Test-Path -LiteralPath $sidecarExe -PathType Leaf)) {
    throw 'Packaged Backend output is missing'
}

Write-Output '[3/6] Explicit development-mode loopback smoke'
$portListener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback,
    0
)
$portListener.Start()
$smokePort = ([System.Net.IPEndPoint]$portListener.LocalEndpoint).Port
$portListener.Stop()
$smokeData = Join-Path $workspaceRoot ".runtime\verify-m1-$PID"
New-Item -ItemType Directory -Path $smokeData -Force | Out-Null
$smokeStdout = Join-Path $smokeData 'packaged-serve.stdout.log'
$smokeStderr = Join-Path $smokeData 'packaged-serve.stderr.log'
$existingSidecarIds = @(
    Get-Process -Name harness-shell-sidecar -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $sidecarExe } |
        Select-Object -ExpandProperty Id
)
$sidecarProcess = Start-Process -FilePath $sidecarExe -ArgumentList @(
    'serve', '--port', "$smokePort", '--data-dir', $smokeData
) -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $smokeStdout `
    -RedirectStandardError $smokeStderr
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
        if ($sidecarProcess.HasExited) { throw 'Packaged Backend exited before readiness' }
        try {
            $response = Invoke-WebRequest -UseBasicParsing `
                -Headers @{ 'X-Request-ID' = [guid]::NewGuid().ToString() } `
                -Uri "http://127.0.0.1:$smokePort/v1/health/live" `
                -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $ready) {
        $startupOutput = @(
            Get-Content -LiteralPath $smokeStdout -Encoding UTF8 -ErrorAction SilentlyContinue
            Get-Content -LiteralPath $smokeStderr -Encoding UTF8 -ErrorAction SilentlyContinue
        ) -join [Environment]::NewLine
        throw "Packaged Backend did not become ready on dynamic port $smokePort`n$startupOutput"
    }
} finally {
    # PyInstaller one-file mode uses a parent and worker. Stop every new
    # process for this exact artifact, while preserving any pre-existing one.
    $smokeProcesses = @(
        Get-Process -Name harness-shell-sidecar -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Path -eq $sidecarExe -and
                $existingSidecarIds -notcontains $_.Id
            }
    )
    if ($smokeProcesses.Count -ne 0) {
        $smokeProcesses | Stop-Process -Force -ErrorAction Stop
    }
}

Write-Output '[4/6] Minimal Tauri shell tests'
& $cargoExe test --manifest-path (Join-Path $tauriRoot 'Cargo.toml') --all-targets --offline
if ($LASTEXITCODE -ne 0) { throw 'Tauri shell tests failed' }

Write-Output '[5/6] Frontend tests and production build'
$npmCache = Join-Path $workspaceRoot '.runtime\npm-cache'
New-Item -ItemType Directory -Path $npmCache -Force | Out-Null
& npm.cmd --prefix $frontendRoot --cache $npmCache ci
if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
& npm.cmd test --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed' }
& npm.cmd run build --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Frontend production build failed' }

Write-Output '[6/6] Tauri capability ownership'
$customPermissionFiles = @(Get-ChildItem -LiteralPath (Join-Path $tauriRoot 'permissions') -File -Filter '*.toml' | Select-Object -ExpandProperty BaseName | Sort-Object)
if (($customPermissionFiles -join ',') -ne 'bootstrap') {
    throw "Unexpected custom Tauri permissions: $($customPermissionFiles -join ', ')"
}
$libSource = Get-Content -LiteralPath (Join-Path $tauriRoot 'src\lib.rs') -Encoding UTF8 -Raw
foreach ($command in @('get_backend_bootstrap')) {
    if (-not $libSource.Contains($command)) { throw "Missing Tauri shell command: $command" }
}
if ($libSource -match 'commands::(?:agent|approval|connections|credentials|diagnostics|runtime|sftp|terminal)') {
    throw 'A removed Rust business command module remains registered'
}

Write-Output 'M1 automated gate passed: local Windows tests, packaged Backend serve smoke, minimal Tauri shell, and frontend build only.'
