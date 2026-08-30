$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$helperPath = Join-Path $workspaceRoot 'scripts\ssh-lab-readiness.ps1'
if (-not (Test-Path -LiteralPath $helperPath -PathType Leaf)) {
    throw 'SSH lab readiness helper is missing'
}
. $helperPath

$script:containerIdQueries = 0
$containerId = Wait-SshLabContainerId `
    -Service 'jump' `
    -Deadline ([DateTime]::UtcNow.AddSeconds(1)) `
    -PollMilliseconds 1 `
    -QueryContainerId {
        param([string]$Service)
        if ($Service -ne 'jump') { throw "Unexpected service: $Service" }
        $script:containerIdQueries += 1
        if ($script:containerIdQueries -eq 1) { return $null }
        return '  container-123  '
    }
if ($containerId -ne 'container-123' -or $script:containerIdQueries -ne 2) {
    throw 'SSH lab readiness must wait for a temporarily absent container ID'
}

try {
    Wait-SshLabContainerId `
        -Service 'target' `
        -Deadline ([DateTime]::UtcNow.AddMilliseconds(-1)) `
        -PollMilliseconds 1 `
        -QueryContainerId { return $null }
    throw 'SSH lab readiness unexpectedly accepted a missing container ID'
} catch {
    if ($_.Exception.Message -ne 'SSH lab container was not created: target') {
        throw
    }
}
