$ErrorActionPreference = 'Stop'

$backendRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $backendRoot
$pythonExe = Join-Path $backendRoot '.venv\Scripts\python.exe'
$specPath = Join-Path $backendRoot 'harness-shell-sidecar.spec'
$buildLock = Join-Path $backendRoot 'build-requirements.lock'
$distExe = Join-Path $backendRoot 'dist\harness-shell-sidecar.exe'
$binariesDir = Join-Path $workspaceRoot 'frontend\src-tauri\binaries'
$smokeScript = Join-Path $PSScriptRoot 'smoke_sidecar.py'
$env:PYTHONHASHSEED = '0'
$env:SOURCE_DATE_EPOCH = '0'

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Python virtual environment is missing: $pythonExe"
}

$pythonVersion = & $pythonExe -c 'import platform; print(platform.python_version())'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12.13') {
    throw "Sidecar build requires Python 3.12.13, found $pythonVersion"
}
$installedPackages = @(& $pythonExe -m pip list --format=freeze)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the Sidecar build environment"
}
$lockedPackages = @(Get-Content -Encoding UTF8 -LiteralPath $buildLock | Where-Object {
    $_ -and -not $_.StartsWith('#')
})
foreach ($lockedPackage in $lockedPackages) {
    if ($installedPackages -notcontains $lockedPackage) {
        throw "Sidecar build dependency does not match lock: $lockedPackage"
    }
}

$rustcCommand = Get-Command rustc.exe -ErrorAction SilentlyContinue
if ($null -eq $rustcCommand) {
    $rustcCandidate = Join-Path $env:USERPROFILE '.cargo\bin\rustc.exe'
    if (-not (Test-Path -LiteralPath $rustcCandidate -PathType Leaf)) {
        throw 'rustc.exe was not found on PATH or in the current user Cargo bin directory'
    }
    $rustcExe = $rustcCandidate
} else {
    $rustcExe = $rustcCommand.Source
}

Push-Location $backendRoot
try {
    & $pythonExe -m PyInstaller --clean --noconfirm $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$rustVersion = & $rustcExe -vV
if ($LASTEXITCODE -ne 0) {
    throw "rustc -vV failed with exit code $LASTEXITCODE"
}
$hostLines = @($rustVersion | Where-Object { $_ -match '^host: (\S+)$' })
if ($hostLines.Count -ne 1) {
    throw "Expected exactly one rustc host line, found $($hostLines.Count)"
}
$targetTriple = [regex]::Match($hostLines[0], '^host: (\S+)$').Groups[1].Value

if (-not (Test-Path -LiteralPath $distExe -PathType Leaf)) {
    throw "PyInstaller output is missing: $distExe"
}
New-Item -ItemType Directory -Force -Path $binariesDir | Out-Null
$targetExe = Join-Path $binariesDir "harness-shell-sidecar-$targetTriple.exe"
Copy-Item -LiteralPath $distExe -Destination $targetExe -Force

& $pythonExe $smokeScript $targetExe
if ($LASTEXITCODE -ne 0) {
    throw "Packaged Sidecar smoke test failed with exit code $LASTEXITCODE"
}

Write-Output $targetExe
