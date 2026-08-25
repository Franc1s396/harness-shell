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

if (Test-Path -LiteralPath (Join-Path $cargoBin 'cargo.exe') -PathType Leaf) {
    $env:Path = "$cargoBin;$env:Path"
}
foreach ($command in @('cargo.exe', 'rustc.exe', 'rustup.exe', 'npm.cmd')) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found on PATH"
    }
}
$activeToolchain = & rustup.exe show active-toolchain
if ($LASTEXITCODE -ne 0 -or $activeToolchain -notmatch '^stable-x86_64-pc-windows-msvc') {
    throw "M1 requires the stable-x86_64-pc-windows-msvc Rust toolchain, found $activeToolchain"
}
$rustHost = & rustc.exe -vV | Where-Object { $_ -match '^host:' }
if ($LASTEXITCODE -ne 0 -or $rustHost -ne 'host: x86_64-pc-windows-msvc') {
    throw "M1 requires the x86_64-pc-windows-msvc rustc host, found $rustHost"
}
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    throw 'Visual Studio Installer vswhere.exe was not found'
}
$msvc = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($msvc -join ''))) {
    throw 'Visual Studio C++ x64/x86 build tools were not found'
}
$windowsSdkLib = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Lib'
$windowsSdk = Get-ChildItem -LiteralPath $windowsSdkLib -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'um\x64') -PathType Container } |
    Select-Object -First 1
if ($null -eq $windowsSdk) {
    throw 'Windows SDK x64 libraries were not found'
}
$webViewRoots = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\EdgeWebView\Application'),
    (Join-Path $env:LOCALAPPDATA 'Microsoft\EdgeWebView\Application')
)
$webView2Executable = $webViewRoots | ForEach-Object {
    Get-ChildItem -LiteralPath $_ -Directory -ErrorAction SilentlyContinue
} | ForEach-Object {
    Join-Path $_.FullName 'msedgewebview2.exe'
} | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf
} | Select-Object -First 1
if ($null -eq $webView2Executable) {
    throw 'Microsoft Edge WebView2 Runtime executable was not found'
}

Write-Output '[1/6] Python environment'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw 'Python 3.12.13 was not found'
    }
    & $pythonCommand.Source -m venv (Join-Path $backendRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$pythonVersion = & $pythonExe -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.13') {
    throw "M1 requires Python 3.12.13, found $pythonVersion"
}
& $pythonExe -m pip install -r (Join-Path $backendRoot 'build-requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Locked Python dependency installation failed' }
& $pythonExe -m pip install --no-deps -e "$backendRoot[dev]"
if ($LASTEXITCODE -ne 0) { throw 'backend[dev] installation failed' }

Write-Output '[2/6] Python tests'
$pytestTemp = Join-Path $env:TEMP "harness-shell-m1-pytest-$PID"
& $pythonExe -m pytest --basetemp $pytestTemp -p no:cacheprovider $backendRoot
if ($LASTEXITCODE -ne 0) { throw 'Python tests failed' }

Write-Output '[3/6] Sidecar package'
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $backendRoot 'scripts\build_sidecar.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Sidecar package build failed' }
$env:HARNESS_SIDECAR_EXE = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'
if (-not (Test-Path -LiteralPath $env:HARNESS_SIDECAR_EXE -PathType Leaf)) {
    throw 'Packaged Sidecar output is missing'
}

Write-Output '[4/6] Rust tests'
& cargo.exe test --manifest-path (Join-Path $tauriRoot 'Cargo.toml') --all-targets
if ($LASTEXITCODE -ne 0) { throw 'Rust tests failed' }

Write-Output '[5/6] Web build'
& npm.cmd ci --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
& npm.cmd run build --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Web build failed' }

Write-Output '[6/6] Tauri prerequisites'
& npm.cmd run tauri info --prefix $frontendRoot
if ($LASTEXITCODE -ne 0) { throw 'Tauri prerequisite inspection failed' }
