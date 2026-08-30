$ErrorActionPreference = "Stop"

$shellScripts = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "*.sh" -File)
if ($shellScripts.Count -eq 0) {
    throw "SSH lab shell scripts are missing"
}

foreach ($script in $shellScripts) {
    $bytes = [System.IO.File]::ReadAllBytes($script.FullName)
    if ([Array]::IndexOf($bytes, [byte]13) -ge 0) {
        throw "SSH lab shell script must use LF line endings: $($script.Name)"
    }
}
