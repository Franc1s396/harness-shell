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
        # Compose 可能在 ps -q 发布容器 ID 前报告启动成功；
        # 只有成功但结果为空的查询才视为仍在等待。
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
