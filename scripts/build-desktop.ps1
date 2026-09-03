$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

Invoke-Checked 'powershell.exe' @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    (Join-Path $workspaceRoot 'backend\scripts\build_sidecar.ps1')
)
Invoke-Checked 'powershell.exe' @(
    '-NoProfile',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    (Join-Path $workspaceRoot 'scripts\build-launcher.ps1')
)
Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'build')
Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'tauri', '--', 'build', '--bundles', 'nsis')

$installerDirectory = Join-Path $workspaceRoot 'frontend\src-tauri\target\release\bundle\nsis'
$installers = @(Get-ChildItem -LiteralPath $installerDirectory -Filter '*.exe' -File -ErrorAction SilentlyContinue)
if ($installers.Count -eq 0) {
    throw "NSIS installer output is missing: $installerDirectory"
}

$installers | Select-Object -ExpandProperty FullName
