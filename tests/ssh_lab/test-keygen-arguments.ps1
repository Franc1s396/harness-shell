$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$startScript = Get-Content -Encoding UTF8 -Raw -LiteralPath (Join-Path $workspaceRoot 'scripts\start-ssh-lab.ps1')
$expectedArgument = '-N ''""'''
if ([regex]::Matches($startScript, [regex]::Escape($expectedArgument)).Count -lt 2) {
    throw 'start-ssh-lab.ps1 must preserve empty -N arguments for Windows PowerShell 5.1'
}

$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$testRoot = [IO.Path]::GetFullPath((Join-Path $tempBase "harness-shell-keygen-test-$PID"))
if ([IO.Path]::GetDirectoryName($testRoot).TrimEnd('\') -ne $tempBase) {
    throw 'keygen test path escaped the temporary directory'
}
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $keyPath = Join-Path $testRoot 'client_ed25519'
    & ssh-keygen.exe -q -t ed25519 -N '""' -C 'harness-m2-keygen-test' -f $keyPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
        throw 'Windows PowerShell did not preserve the empty ssh-keygen passphrase argument'
    }
    & ssh-keygen.exe -y -P '""' -f $keyPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Generated SSH key unexpectedly requires a passphrase'
    }
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
