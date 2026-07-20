param(
    [string]$CamId,
    [string]$SshTarget,
    [string]$SshConfigFile,
    [switch]$ConfirmRevocation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$script:DeprovisionSshTargetWasSpecified = $PSBoundParameters.ContainsKey('SshTarget')
$SshConfigArguments = @()
if ($SshConfigFile) {
    if (-not (Test-Path -LiteralPath $SshConfigFile -PathType Leaf)) {
        throw "SSH config file does not exist: $SshConfigFile"
    }
    $SshConfigFile = (Resolve-Path -LiteralPath $SshConfigFile).Path
    $SshConfigArguments = @('-F', $SshConfigFile)
}

function Assert-DeprovisionCamId {
    param([string]$Value)
    if (-not $Value -or $Value.Length -gt 64 -or $Value -notmatch '^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$') {
        throw 'Invalid CAM id.'
    }
}

function Normalize-DeprovisionCamId {
    param([string]$Value)
    return $Value.Trim().ToLowerInvariant()
}

function Resolve-DeprovisionPromptValue {
    param([AllowNull()][string]$Value, [string]$DefaultValue, [string]$PromptText, [bool]$WasSpecified)
    if ($WasSpecified) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return $DefaultValue }
        return $Value
    }
    $answer = Read-Host "$PromptText [$DefaultValue]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultValue }
    return $answer
}

function Assert-DeprovisionSshTarget {
    param([string]$Target)
    if (-not $Target -or $Target.StartsWith('-') -or $Target -notmatch '^[A-Za-z0-9_.@:-]+$') {
        throw 'Invalid SSH target.'
    }
}

function Resolve-DeprovisionSshTarget {
    param([string]$Target)
    Assert-DeprovisionSshTarget $Target
    $lines = @(& ssh.exe @SshConfigArguments -G $Target)
    if ($LASTEXITCODE -ne 0) { throw "ssh.exe could not resolve '$Target'." }
    $required = @('hostname ', 'user ', 'port ')
    foreach ($prefix in $required) {
        if (-not ($lines | Where-Object { $_.StartsWith($prefix) })) {
            throw "ssh.exe returned an incomplete resolution for '$Target'."
        }
    }
}

function Invoke-DeprovisionSshJson {
    param([string[]]$SshArguments, [AllowEmptyString()][string]$Json = '')

    $output = if ($Json.Length -gt 0) { @($Json | & ssh.exe @SshConfigArguments @SshArguments) } else { @(& ssh.exe @SshConfigArguments @SshArguments) }
    return [pscustomobject]@{ StdOut = ($output -join [Environment]::NewLine); ExitCode = $LASTEXITCODE }
}

function New-CamDeprovisionRequest {
    param([string]$CamId, [string]$Fingerprint)
    Assert-DeprovisionCamId $CamId
    if (-not $Fingerprint.StartsWith('SHA256:') -or $Fingerprint -match "[`r`n]") { throw 'Invalid SHA256 fingerprint.' }
    return [ordered]@{
        cam_id = $CamId
        expected_fingerprint = $Fingerprint
        confirm = $true
    } | ConvertTo-Json -Compress
}

function Read-CamDeprovisionResponse {
    param([string]$StdOut, [int]$NativeExitCode)
    try { $response = $StdOut | ConvertFrom-Json } catch { throw "CAM server returned invalid JSON (ssh.exe exit $NativeExitCode)." }
    if (-not $response.status) { throw 'CAM server response omitted status.' }
    return $response
}

function Assert-CamDeprovisionOutcome {
    param($Response, [int]$NativeExitCode, [string]$CamId, [string]$Fingerprint)
    if (-not $CamId) { $CamId = $Response.cam_id }
    if (-not $Fingerprint) { $Fingerprint = $Response.fingerprint }
    if ($Response.status -isnot [string] -or $Response.cam_id -isnot [string] -or
        $Response.fingerprint -isnot [string] -or $Response.access_revoked -isnot [bool] -or
        $Response.cam_id -ne $CamId -or $Response.fingerprint -ne $Fingerprint) {
        throw 'Deprovision response has invalid types or does not match the exact CAM and fingerprint.'
    }
    switch ($Response.status) {
        'SUCCESS' {
            if ($NativeExitCode -ne 0 -or $Response.access_revoked -ne $true) { throw 'SUCCESS requires exit 0 and access_revoked=true.' }
            return
        }
        'FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED' {
            if ($NativeExitCode -eq 1 -and $Response.access_revoked -eq $true) {
                throw 'CAM access was revoked, but server cleanup remains required.'
            }
            throw 'HIGH SEVERITY: cleanup-required response did not confirm access revocation.'
        }
        'FAILED_REVOCATION_UNCONFIRMED' {
            if ($NativeExitCode -ne 1) { throw 'FAILED_REVOCATION_UNCONFIRMED requires exit 1.' }
            throw 'HIGH SEVERITY: the server could not confirm access revocation.'
        }
        default { throw "Unknown CAM deprovision status '$($Response.status)' (ssh.exe exit $NativeExitCode)." }
    }
}

function Assert-CamShowResponse {
    param($Response, [int]$NativeExitCode, [string]$CamId)
    if ($NativeExitCode -ne 0 -or $Response.status -isnot [string] -or $Response.status -ne 'SUCCESS' -or
        $Response.cam_id -isnot [string] -or $Response.cam_id -ne $CamId -or
        $Response.customers -isnot [array] -or @($Response.customers | Where-Object { $_ -isnot [int] -and $_ -isnot [long] }).Count -ne 0 -or
        $Response.allow_delivery -isnot [bool] -or $Response.fingerprint -isnot [string]) {
        throw 'Show response has an invalid status/exit pair, type, or CAM identity.'
    }
}

function Invoke-Deprovision {
    param([string]$CamId, [string]$SshTarget, [switch]$ConfirmRevocation)

    $CamId = Normalize-DeprovisionCamId $CamId
    Assert-DeprovisionCamId $CamId
    Resolve-DeprovisionSshTarget $SshTarget
    $showCommand = "sudo /opt/logan-mcp/bin/cam-admin show --cam $CamId --json"
    $shownResult = Invoke-DeprovisionSshJson -SshArguments @($SshTarget, $showCommand)
    try { $shown = $shownResult.StdOut | ConvertFrom-Json } catch { throw 'Unable to read the current CAM assignment from the server.' }
    Assert-CamShowResponse -Response $shown -NativeExitCode $shownResult.ExitCode -CamId $CamId
    Write-Host "CAM: $($shown.cam_id)"
    Write-Host "Customers: $(@($shown.customers) -join ', ')"
    Write-Host "Allow delivery: $($shown.allow_delivery)"
    Write-Host "Fingerprint: $($shown.fingerprint)"

    if (-not $ConfirmRevocation) {
        $confirmation = Read-Host "Type REVOKE $CamId to continue"
        if ($confirmation -cne "REVOKE $CamId") { throw 'Deprovision cancelled; confirmation text did not match.' }
    }

    $json = New-CamDeprovisionRequest -CamId $CamId -Fingerprint $shown.fingerprint
    $result = Invoke-DeprovisionSshJson -SshArguments @($SshTarget, 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json') -Json $json
    $response = Read-CamDeprovisionResponse -StdOut $result.StdOut -NativeExitCode $result.ExitCode
    Assert-CamDeprovisionOutcome -Response $response -NativeExitCode $result.ExitCode -CamId $CamId -Fingerprint $shown.fingerprint
    Write-Host "CAM access revoked: $CamId"
}

function Invoke-DeprovisionEntrypoint {
    if (-not $CamId) { $script:CamId = Read-Host 'CAM id' }
    $script:CamId = Normalize-DeprovisionCamId $CamId
    $script:SshTarget = Resolve-DeprovisionPromptValue -Value $SshTarget -DefaultValue 'automation1' -PromptText 'Administrator SSH target' -WasSpecified $script:DeprovisionSshTargetWasSpecified
    Invoke-Deprovision -CamId $CamId -SshTarget $SshTarget -ConfirmRevocation:$ConfirmRevocation
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-DeprovisionEntrypoint
}
