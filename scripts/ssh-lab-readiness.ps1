function Wait-SshLabContainerId {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Service,
        [Parameter(Mandatory = $true)]
        [DateTime]$Deadline,
        [Parameter(Mandatory = $true)]
        [scriptblock]$QueryContainerId,
        [ValidateRange(0, 10000)]
        [int]$PollMilliseconds = 500
    )

    do {
        # Compose may report successful startup before `ps -q` publishes the
        # container ID. Treat only an empty successful query as pending.
        $candidate = & $QueryContainerId $Service
        $containerId = if ($null -eq $candidate) { '' } else { [string]$candidate }
        if (-not [string]::IsNullOrWhiteSpace($containerId)) {
            return $containerId.Trim()
        }
        if ([DateTime]::UtcNow -ge $Deadline) { break }
        Start-Sleep -Milliseconds $PollMilliseconds
    } while ($true)

    throw "SSH lab container was not created: $Service"
}
