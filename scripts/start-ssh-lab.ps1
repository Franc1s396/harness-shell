$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$labRoot = [IO.Path]::GetFullPath((Join-Path $workspaceRoot 'tests\ssh_lab'))
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $labRoot '.runtime'))
$expectedParent = [IO.Path]::GetFullPath($labRoot).TrimEnd('\')
if ([IO.Path]::GetDirectoryName($runtimeRoot).TrimEnd('\') -ne $expectedParent) {
    throw 'SSH lab runtime path escaped the expected lab directory'
}

foreach ($command in @('docker.exe', 'docker-compose.exe', 'ssh-keygen.exe')) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found on PATH"
    }
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

if (Test-Path -LiteralPath $runtimeRoot) {
    Remove-Item -LiteralPath $runtimeRoot -Recurse -Force
}
$jumpRoot = New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'jump')
$targetRoot = New-Item -ItemType Directory -Path (Join-Path $runtimeRoot 'target')

function New-RuntimeSecret([string]$Prefix) {
    $bytes = [byte[]]::new(24)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
    } finally {
        $random.Dispose()
    }
    $hex = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
    return "$Prefix-$hex"
}

$jumpPassword = New-RuntimeSecret 'm2-jump'
$targetPassword = New-RuntimeSecret 'm2-target'
$keyPassphrase = New-RuntimeSecret 'm2-key'
$plainKey = Join-Path $runtimeRoot 'client_unencrypted_ed25519'
$encryptedKey = Join-Path $runtimeRoot 'client_encrypted_ed25519'

& ssh-keygen.exe -q -t ed25519 -N '""' -C 'harness-m2-unencrypted' -f $plainKey
if ($LASTEXITCODE -ne 0) { throw 'Unencrypted client key generation failed' }
& ssh-keygen.exe -q -t ed25519 -N $keyPassphrase -C 'harness-m2-encrypted' -f $encryptedKey
if ($LASTEXITCODE -ne 0) { throw 'Encrypted client key generation failed' }

foreach ($nodeRoot in @($jumpRoot.FullName, $targetRoot.FullName)) {
    $hostKey = Join-Path $nodeRoot 'host_ed25519_key'
    & ssh-keygen.exe -q -t ed25519 -N '""' -C 'harness-m2-host' -f $hostKey
    if ($LASTEXITCODE -ne 0) { throw "Host key generation failed for $nodeRoot" }
    $authorizedKeys = @(
        [IO.File]::ReadAllText("$plainKey.pub").Trim(),
        [IO.File]::ReadAllText("$encryptedKey.pub").Trim()
    ) -join "`n"
    [IO.File]::WriteAllText(
        (Join-Path $nodeRoot 'authorized_keys'),
        "$authorizedKeys`n",
        [Text.UTF8Encoding]::new($false)
    )
}

function Get-Fingerprint([string]$PublicKeyPath) {
    $line = & ssh-keygen.exe -E sha256 -lf $PublicKeyPath
    if ($LASTEXITCODE -ne 0 -or $line -notmatch '^\d+\s+(SHA256:\S+)') {
        throw "Unable to calculate fingerprint for $PublicKeyPath"
    }
    return $Matches[1]
}

$manifest = [ordered]@{
    jump_host = '127.0.0.1'
    jump_port = 2222
    jump_username = 'jumpuser'
    target_host = 'target'
    target_port = 22
    target_username = 'targetuser'
    unencrypted_private_key_path = $plainKey
    encrypted_private_key_path = $encryptedKey
    jump_host_fingerprint = Get-Fingerprint (Join-Path $jumpRoot.FullName 'host_ed25519_key.pub')
    target_host_fingerprint = Get-Fingerprint (Join-Path $targetRoot.FullName 'host_ed25519_key.pub')
    target_permission_denied_root = '/srv/harness-sftp-denied'
    target_cross_device_root = '/srv/harness-sftp-cross-device'
}
$secrets = [ordered]@{
    jump_password = $jumpPassword
    target_password = $targetPassword
    private_key_passphrase = $keyPassphrase
}
[IO.File]::WriteAllText(
    (Join-Path $runtimeRoot 'manifest.json'),
    ($manifest | ConvertTo-Json -Depth 3),
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $runtimeRoot 'secrets.json'),
    ($secrets | ConvertTo-Json -Depth 3),
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $runtimeRoot 'lab.env'),
    "JUMP_PASSWORD=$jumpPassword`nTARGET_PASSWORD=$targetPassword`n",
    [Text.UTF8Encoding]::new($false)
)

Push-Location $labRoot
try {
    & docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 down --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw 'Existing SSH lab cleanup failed' }
    & docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 up --build --force-recreate --detach
    if ($LASTEXITCODE -ne 0) { throw 'SSH lab startup failed' }

    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    $jumpContainerId = $null
    foreach ($service in @('jump', 'target')) {
        $containerId = (& docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 ps -q $service).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($containerId)) {
            throw "SSH lab container was not created: $service"
        }
        if ($service -eq 'jump') { $jumpContainerId = $containerId }
        do {
            $health = (& docker.exe inspect --format '{{.State.Health.Status}}' $containerId).Trim()
            if ($health -eq 'healthy') { break }
            if ($health -eq 'unhealthy') { throw "SSH lab health check failed: $service" }
            Start-Sleep -Milliseconds 500
        } while ([DateTime]::UtcNow -lt $deadline)
        if ($health -ne 'healthy') { throw "SSH lab health check timed out: $service" }
    }

    $publishedPort = (& docker.exe port $jumpContainerId '22/tcp').Trim()
    if ($LASTEXITCODE -ne 0 -or $publishedPort -ne '127.0.0.1:2222') {
        throw 'SSH lab jump port was not published on 127.0.0.1:2222'
    }
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect('127.0.0.1', 2222, $null, $null)
        try {
            if (-not $connect.AsyncWaitHandle.WaitOne(5000)) {
                throw 'SSH lab jump port did not accept a connection within 5 seconds'
            }
            $client.EndConnect($connect)
        } finally {
            $connect.AsyncWaitHandle.Close()
        }
    } finally {
        $client.Dispose()
    }
} catch {
    & docker-compose.exe --env-file .runtime\lab.env --project-name harness-shell-m2 down --remove-orphans | Out-Null
    throw
} finally {
    Pop-Location
}

Write-Output 'SSH lab ready: jump=127.0.0.1:2222 target=compose-network-only'
