$ErrorActionPreference = 'Stop'

$labRoot = $PSScriptRoot
$composeFile = Join-Path $labRoot 'docker-compose.yml'

$env:JUMP_PASSWORD = 'topology-test-jump'
$env:TARGET_PASSWORD = 'topology-test-target'
try {
    $json = & docker-compose.exe -f $composeFile --project-directory $labRoot config --format json
    if ($LASTEXITCODE -ne 0) { throw 'Unable to resolve the SSH lab Compose topology' }
    $config = $json | ConvertFrom-Json
} finally {
    Remove-Item Env:JUMP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TARGET_PASSWORD -ErrorAction SilentlyContinue
}

$jumpNetworks = @($config.services.jump.networks.PSObject.Properties.Name)
$targetNetworks = @($config.services.target.networks.PSObject.Properties.Name)
if ($jumpNetworks -notcontains 'ssh_ingress') {
    throw 'The jump service requires a non-internal ingress network for its published port'
}
if ($jumpNetworks -notcontains 'ssh_lab') {
    throw 'The jump service must remain attached to the internal SSH lab network'
}
if ($targetNetworks.Count -ne 1 -or $targetNetworks[0] -ne 'ssh_lab') {
    throw 'The target service must be reachable only from the internal SSH lab network'
}
if ($config.networks.ssh_lab.internal -ne $true) {
    throw 'The SSH lab network must remain internal'
}
if ($null -eq $config.networks.ssh_ingress -or $config.networks.ssh_ingress.internal -eq $true) {
    throw 'The ingress network must allow Docker to publish the jump port'
}

$publishedPorts = @($config.services.jump.ports)
if ($publishedPorts.Count -ne 1) {
    throw 'The jump service must publish exactly one port'
}
$published = $publishedPorts[0]
if ($published.host_ip -ne '127.0.0.1' -or [int]$published.published -ne 2222 -or [int]$published.target -ne 22) {
    throw 'The jump service must publish 127.0.0.1:2222 to container port 22'
}
