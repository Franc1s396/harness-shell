$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$tauriConfigPath = Join-Path $workspaceRoot 'frontend\src-tauri\tauri.conf.json'
$installerTemplatePath = Join-Path $workspaceRoot 'frontend\src-tauri\windows\installer.nsi'

if (-not (Test-Path -LiteralPath $installerTemplatePath -PathType Leaf)) {
    throw "NSIS installer template is missing: $installerTemplatePath"
}

$config = Get-Content -Encoding UTF8 -LiteralPath $tauriConfigPath | ConvertFrom-Json
$template = Get-Content -Encoding UTF8 -Raw -LiteralPath $installerTemplatePath

if ($config.mainBinaryName -ne 'harness-shell-ui') {
    throw 'Tauri mainBinaryName must be harness-shell-ui'
}
$targets = @($config.bundle.targets)
if ($targets.Count -ne 1 -or $targets[0] -ne 'nsis') {
    throw 'Tauri bundle target must be exactly nsis'
}
$externalBinaries = @($config.bundle.externalBin)
foreach ($expected in @('binaries/harness-shell-sidecar', 'binaries/harness-shell-launcher')) {
    if ($externalBinaries -notcontains $expected) {
        throw "Tauri bundle inputs are missing $expected"
    }
}
if ($config.bundle.windows.nsis.template -ne './windows/installer.nsi') {
    throw 'Tauri NSIS template path is not pinned to ./windows/installer.nsi'
}

$startMenuDeclarations = @(
    [regex]::Matches(
        $template,
        '(?m)^\s*CreateShortcut\s+"\$SMPROGRAMS\\Harness Shell\.lnk"\s+"\$INSTDIR\\harness-shell-launcher\.exe"\s*$'
    )
)
if ($startMenuDeclarations.Count -ne 1) {
    throw "Expected exactly one Launcher Start Menu shortcut declaration, found $($startMenuDeclarations.Count)"
}
if ($template -match '(?im)^\s*CreateShortcut\s+.*harness-shell-(?:ui|sidecar)\.exe') {
    throw 'UI or Backend must not own an installer shortcut'
}
$finishLaunches = @(
    [regex]::Matches(
        $template,
        '(?m)^\s*nsis_tauri_utils::RunAsUser\s+"\$INSTDIR\\harness-shell-launcher\.exe"'
    )
)
if ($finishLaunches.Count -ne 2) {
    throw "Expected GUI and silent finish actions to launch only the Launcher, found $($finishLaunches.Count)"
}

Write-Output 'Installer entry verification passed.'
