$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $workspaceRoot 'launcher\Cargo.toml'
$sourceExe = Join-Path $workspaceRoot 'launcher\target\x86_64-pc-windows-msvc\release\harness-shell-launcher.exe'
$binariesDir = Join-Path $workspaceRoot 'frontend\src-tauri\binaries'
$targetExe = Join-Path $binariesDir 'harness-shell-launcher-x86_64-pc-windows-msvc.exe'

$rustVersion = & rustc.exe -vV
if ($LASTEXITCODE -ne 0) {
    throw "rustc -vV failed with exit code $LASTEXITCODE"
}
$hostLines = @($rustVersion | Where-Object { $_ -match '^host: (\S+)$' })
if ($hostLines.Count -ne 1) {
    throw "Expected exactly one rustc host line, found $($hostLines.Count)"
}
$targetTriple = [regex]::Match($hostLines[0], '^host: (\S+)$').Groups[1].Value
if ($targetTriple -ne 'x86_64-pc-windows-msvc') {
    throw "Launcher build supports only x86_64-pc-windows-msvc, found $targetTriple"
}

& cargo.exe build --offline --locked --release --target $targetTriple --manifest-path $manifestPath
if ($LASTEXITCODE -ne 0) {
    throw "Launcher Cargo build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf)) {
    throw "Launcher build output is missing: $sourceExe"
}

New-Item -ItemType Directory -Force -Path $binariesDir | Out-Null
Copy-Item -LiteralPath $sourceExe -Destination $targetExe -Force
if (-not (Test-Path -LiteralPath $targetExe -PathType Leaf)) {
    throw "Packaged Launcher companion is missing: $targetExe"
}

Write-Output $targetExe
