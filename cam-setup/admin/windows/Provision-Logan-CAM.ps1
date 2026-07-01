param(
    [string]$CamId,
    [object[]]$Customers,
    [object]$AllowDelivery = $false,
    [string]$SshTarget,
    [string]$OutputDir
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$script:AllowDeliveryWasSpecified = $PSBoundParameters.ContainsKey('AllowDelivery')
$script:SshTargetWasSpecified = $PSBoundParameters.ContainsKey('SshTarget')
$script:OutputDirWasSpecified = $PSBoundParameters.ContainsKey('OutputDir')
$script:CamSetupRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path

function ConvertTo-CamBoolean {
    param([Parameter(Mandatory = $true)][object]$Value)
    if ($Value -is [bool]) { return [bool]$Value }
    if ($Value -is [string] -and $Value -ceq 'true') { return $true }
    if ($Value -is [string] -and $Value -ceq 'false') { return $false }
    throw 'AllowDelivery must be exactly true or false.'
}

function Normalize-CamId {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Trim().ToLowerInvariant()
}

function Resolve-CamPromptValue {
    param([AllowNull()][string]$Value, [string]$DefaultValue, [string]$PromptText, [bool]$WasSpecified)
    if ($WasSpecified) {
        if ([string]::IsNullOrWhiteSpace($Value)) { return $DefaultValue }
        return $Value
    }
    $answer = Read-Host "$PromptText [$DefaultValue]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultValue }
    return $answer
}

function Assert-ValidCamId {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Length -gt 64 -or $Value -notmatch '^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$') {
        throw "Invalid CAM id '$Value'. Use 1-64 lowercase letters or digits with single '.', '_' or '-' separators."
    }
}

function Assert-ValidCustomers {
    param([Parameter(Mandatory = $true)][object[]]$Value)

    if ($Value.Count -eq 0) {
        throw 'Customers must contain at least one positive integer.'
    }
    foreach ($customer in $Value) {
        if ($customer -isnot [int] -and $customer -isnot [long]) {
            throw 'Every customer must be a JSON integer, not a string or boolean.'
        }
        if ([long]$customer -le 0 -or [long]$customer -gt [int]::MaxValue) {
            throw 'Every customer must be a positive 32-bit integer.'
        }
    }
}

function Assert-ValidAllowDelivery {
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($Value -isnot [bool]) {
        throw 'AllowDelivery must be a boolean.'
    }
}

function Assert-SafeSshTarget {
    param([Parameter(Mandatory = $true)][string]$Target)

    if ([string]::IsNullOrWhiteSpace($Target) -or $Target.StartsWith('-') -or
        $Target -notmatch '^[A-Za-z0-9_.@:-]+$') {
        throw 'SSH target must be one host or user@host token without options, whitespace, or shell characters.'
    }
}

function Resolve-CamSshTarget {
    param([Parameter(Mandatory = $true)][string]$Target)

    Assert-SafeSshTarget $Target
    $lines = @(& ssh.exe -G $Target)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "ssh.exe could not resolve '$Target' (exit $exitCode)."
    }
    $resolved = @{}
    foreach ($line in $lines) {
        if ($line -match '^(hostname|user|port)\s+(.+)$') {
            $resolved[$matches[1]] = $matches[2].Trim()
        }
    }
    $port = 0
    if (-not $resolved.hostname -or -not $resolved.user -or
        -not [int]::TryParse([string]$resolved.port, [ref]$port) -or $port -le 0) {
        throw "ssh.exe returned an incomplete resolution for '$Target'."
    }
    return [pscustomobject]@{ Host = $resolved.hostname; User = $resolved.user; Port = $port }
}

function Assert-CamOutputAvailable {
    param(
        [Parameter(Mandatory = $true)][string]$CamId,
        [Parameter(Mandatory = $true)][string]$OutputDir
    )

    Assert-ValidCamId $CamId
    $finalPath = Join-Path $OutputDir ("logan-cam-$CamId")
    $archivePath = "$finalPath.zip"
    if ((Test-Path -LiteralPath $finalPath) -or (Test-Path -LiteralPath $archivePath)) {
        throw "Output directory or archive already exists and will not be overwritten: $finalPath"
    }
    return $finalPath
}

function New-ProvisionPrivateSecurity {
    param([switch]$Directory)
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
    $acl.SetOwner($currentSid)
    $acl.SetAccessRuleProtection($true, $false)
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $administratorsSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $inheritance = if ($Directory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
    $propagation = [Security.AccessControl.PropagationFlags]::None
    foreach ($entry in @(
        [pscustomobject]@{ Sid = $currentSid; Rights = [Security.AccessControl.FileSystemRights]::FullControl },
        [pscustomobject]@{ Sid = $systemSid; Rights = [Security.AccessControl.FileSystemRights]::Read },
        [pscustomobject]@{ Sid = $administratorsSid; Rights = [Security.AccessControl.FileSystemRights]::Read }
    )) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($entry.Sid, $entry.Rights, $inheritance, $propagation, [Security.AccessControl.AccessControlType]::Allow)
        [void]$acl.AddAccessRule($rule)
    }
    return $acl
}

function Protect-CamPrivateKey {
    param([Parameter(Mandatory = $true)][string]$Path)
    Set-Acl -LiteralPath $Path -AclObject (New-ProvisionPrivateSecurity)
    $applied = Get-Acl -LiteralPath $Path
    if (-not $applied.AreAccessRulesProtected -or @($applied.Access).Count -ne 3) { throw "Failed to replace the DACL on '$Path'." }
}

function Protect-CamPrivateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    Set-Acl -LiteralPath $Path -AclObject (New-ProvisionPrivateSecurity -Directory)
    $applied = Get-Acl -LiteralPath $Path
    if (-not $applied.AreAccessRulesProtected -or @($applied.Access).Count -ne 3) { throw "Failed to replace the directory DACL on '$Path'." }
}

function Assert-CamTemplateContract {
    param([string]$Path, [string[]]$Tokens)
    $content = [IO.File]::ReadAllText($Path)
    foreach ($token in $Tokens) {
        if (-not $content.Contains($token)) { throw "Template is missing required token $token`: $Path" }
        $content = $content.Replace($token, '')
    }
    if ($content -match '@@CAM_') { throw "Template contains an unsupported CAM token: $Path" }
}

function Assert-CamPreflight {
    param([string]$CamId, [string]$OutputDir)
    foreach ($command in @('ssh.exe', 'ssh-keygen.exe')) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Required local command is unavailable: $command" }
    }
    foreach ($template in @(
        (Join-Path $script:CamSetupRoot 'bundle/macos/Install-Logan-MCP.command'),
        (Join-Path $script:CamSetupRoot 'bundle/windows/Double-Click-to-Install.cmd'),
        (Join-Path $script:CamSetupRoot 'bundle/windows/Install-Logan-MCP.ps1'),
        (Join-Path $script:CamSetupRoot 'bundle/README.html')
    )) { if (-not (Test-Path -LiteralPath $template -PathType Leaf)) { throw "Required bundle template is missing: $template" } }
    $installerTokens = @('@@CAM_HOST@@', '@@CAM_PORT@@', '@@CAM_REMOTE_USER@@', '@@CAM_HOST_PUBLIC_KEY@@')
    Assert-CamTemplateContract -Path (Join-Path $script:CamSetupRoot 'bundle/macos/Install-Logan-MCP.command') -Tokens $installerTokens
    Assert-CamTemplateContract -Path (Join-Path $script:CamSetupRoot 'bundle/windows/Install-Logan-MCP.ps1') -Tokens $installerTokens
    Assert-CamTemplateContract -Path (Join-Path $script:CamSetupRoot 'bundle/README.html') -Tokens @('@@CAM_ID@@', '@@CAM_SERVER_NAME@@', '@@CAM_CREATED_AT@@', '@@CAM_FINGERPRINT@@')
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    $probe = Join-Path $OutputDir ('.write-probe.' + [guid]::NewGuid().ToString('N'))
    try { [IO.File]::WriteAllText($probe, 'probe') } finally { if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force } }
    [void](Assert-CamOutputAvailable -CamId $CamId -OutputDir $OutputDir)
    $staging = Join-Path $OutputDir ('.logan-cam-private.' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Protect-CamPrivateDirectory -Path $staging
        return $staging
    }
    catch {
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
        throw
    }
}

function New-CamProvisionRequest {
    param(
        [Parameter(Mandatory = $true)][string]$CamId,
        [Parameter(Mandatory = $true)][object[]]$Customers,
        [Parameter(Mandatory = $true)][object]$AllowDelivery,
        [Parameter(Mandatory = $true)][string]$PublicKey
    )

    Assert-ValidCamId $CamId
    Assert-ValidCustomers $Customers
    Assert-ValidAllowDelivery $AllowDelivery
    if ([string]::IsNullOrWhiteSpace($PublicKey) -or $PublicKey -match "[`r`n]") {
        throw 'PublicKey must be one non-empty OpenSSH line.'
    }
    $integerCustomers = @($Customers | ForEach-Object { [int]$_ })
    return [ordered]@{
        cam_id = $CamId
        customers = $integerCustomers
        allow_delivery = [bool]$AllowDelivery
        public_key = $PublicKey
    } | ConvertTo-Json -Compress
}

function New-CamDeprovisionJson {
    param([string]$CamId, [string]$Fingerprint)

    return [ordered]@{
        cam_id = $CamId
        expected_fingerprint = $Fingerprint
        confirm = $true
    } | ConvertTo-Json -Compress
}

function Invoke-ProvisionSshJson {
    param(
        [Parameter(Mandatory = $true)][string[]]$SshArguments,
        [Parameter(Mandatory = $true)][string]$Json
    )

    $output = @($Json | & ssh.exe @SshArguments)
    return [pscustomobject]@{ StdOut = ($output -join [Environment]::NewLine); ExitCode = $LASTEXITCODE }
}

function Read-CamProvisionResponse {
    param([string]$StdOut, [int]$NativeExitCode)

    try {
        return $StdOut | ConvertFrom-Json
    }
    catch {
        throw "CAM server returned invalid JSON (ssh.exe exit $NativeExitCode)."
    }
}

function Assert-CamProvisionResponse {
    param(
        [Parameter(Mandatory = $true)]$Response,
        [Parameter(Mandatory = $true)][int]$NativeExitCode,
        [Parameter(Mandatory = $true)][string]$CamId,
        [Parameter(Mandatory = $true)][object[]]$Customers,
        [Parameter(Mandatory = $true)][object]$AllowDelivery,
        [Parameter(Mandatory = $true)][string]$Fingerprint
    )

    if ($Response.status -isnot [string] -or $Response.status -ne 'SUCCESS' -or $NativeExitCode -ne 0 -or
        $Response.cam_id -isnot [string] -or $Response.cam_id -ne $CamId -or
        $Response.customers -isnot [array] -or @($Response.customers | Where-Object { $_ -isnot [int] -and $_ -isnot [long] }).Count -ne 0 -or
        $Response.allow_delivery -isnot [bool] -or $Response.fingerprint -isnot [string]) {
        throw 'Provision response has an invalid status/exit pair or JSON field type.'
    }
    $expectedCustomers = @($Customers | ForEach-Object { [int]$_ })
    $actualCustomers = @($Response.customers | ForEach-Object { [int]$_ })
    if ($Response.cam_id -ne $CamId -or
        ($actualCustomers -join ',') -ne ($expectedCustomers -join ',') -or
        [bool]$Response.allow_delivery -ne [bool]$AllowDelivery -or
        $Response.fingerprint -ne $Fingerprint) {
        throw 'Provision response did not exactly match the requested CAM, customers, delivery policy, and local key fingerprint.'
    }
    if (-not $Response.connection -or -not $Response.connection.host -or
        $Response.connection.host -isnot [string] -or $Response.connection.remote_user -isnot [string] -or
        $Response.connection.host_public_key -isnot [string] -or
        ($Response.connection.port -isnot [int] -and $Response.connection.port -isnot [long]) -or
        [long]$Response.connection.port -le 0 -or [long]$Response.connection.port -gt 65535) {
        throw 'Provision response did not contain complete connection metadata.'
    }
}

function Assert-SafeTemplateValue {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value -match "[`r`n]" -or $Value.Contains('@@') -or $Value.Contains("'")) {
        throw 'Template values may not contain newlines or token delimiters.'
    }
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding($false)))
}

function New-CamZipArchive {
    param([string]$SourceDirectory, [string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $modes = @{
        'logan-cam.key' = 33152
        'Install-Logan-MCP.command' = 33261
        'Double-Click-to-Install.cmd' = 33188
        'Install-Logan-MCP.ps1' = 33188
        'README.html' = 33188
    }
    $archive = [IO.Compression.ZipFile]::Open($ArchivePath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($name in @('logan-cam.key', 'Install-Logan-MCP.command', 'Double-Click-to-Install.cmd', 'Install-Logan-MCP.ps1', 'README.html')) {
            $source = Join-Path $SourceDirectory $name
            $entry = $archive.CreateEntry($name, [IO.Compression.CompressionLevel]::Optimal)
            $entry.ExternalAttributes = [int]($modes[$name] -shl 16)
            $input = [IO.File]::OpenRead($source)
            $output = $entry.Open()
            try { $input.CopyTo($output) } finally { $output.Dispose(); $input.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}

function Get-CamZipEntryMetadata {
    param([string]$ArchivePath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $archive.Entries) {
            $raw = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$entry.ExternalAttributes), 0)
            [pscustomobject]@{ Name = $entry.FullName; UnixMode = [int]($raw -shr 16) }
        }
    }
    finally { $archive.Dispose() }
}

function Remove-CamStageRootBestEffort {
    param([string]$StageRoot)
    Remove-Item -LiteralPath $StageRoot -Force
}

function Render-CamTemplate {
    param([string]$TemplatePath, [string]$DestinationPath, [hashtable]$Values)

    if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) {
        throw "Required bundle template is missing: $TemplatePath"
    }
    $content = [IO.File]::ReadAllText($TemplatePath)
    foreach ($token in $Values.Keys) {
        Assert-SafeTemplateValue ([string]$Values[$token])
        $content = $content.Replace($token, [string]$Values[$token])
    }
    if ($content -match '@@CAM_') {
        throw "Template still contains an unresolved CAM token: $TemplatePath"
    }
    Write-Utf8NoBom -Path $DestinationPath -Content $content
}

function Publish-CamBundle {
    param(
        [Parameter(Mandatory = $true)][string]$CamId,
        [Parameter(Mandatory = $true)][string]$OutputDir,
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)]$Connection,
        [string]$Fingerprint = ''
    )

    $finalPath = Assert-CamOutputAvailable -CamId $CamId -OutputDir $OutputDir
    if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
        throw 'Generated private key is missing.'
    }
    $port = [int]$Connection.port
    if (-not $Connection.host -or $Connection.host -notmatch '^[A-Za-z0-9._:-]+$' -or
        $Connection.remote_user -ne 'cam' -or
        -not $Connection.host_public_key -or $port -le 0 -or $port -gt 65535) {
        throw 'Server connection metadata is incomplete.'
    }

    $serverName = if ($Connection.PSObject.Properties.Name -contains 'server_name' -and $Connection.server_name) {
        [string]$Connection.server_name
    } else { [string]$Connection.host }
    $installerValues = @{
        '@@CAM_HOST@@' = [string]$Connection.host
        '@@CAM_PORT@@' = [string]$port
        '@@CAM_REMOTE_USER@@' = [string]$Connection.remote_user
        '@@CAM_HOST_PUBLIC_KEY@@' = [string]$Connection.host_public_key
    }
    $readmeValues = @{
        '@@CAM_ID@@' = [Net.WebUtility]::HtmlEncode($CamId)
        '@@CAM_SERVER_NAME@@' = [Net.WebUtility]::HtmlEncode($serverName)
        '@@CAM_CREATED_AT@@' = [Net.WebUtility]::HtmlEncode([DateTime]::UtcNow.ToString('o'))
        '@@CAM_FINGERPRINT@@' = [Net.WebUtility]::HtmlEncode($Fingerprint)
    }

    if (-not (Test-Path -LiteralPath $OutputDir -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    }
    $stageRoot = Join-Path $OutputDir ('.logan-cam-{0}.{1}.stage' -f $CamId, [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    try { Protect-CamPrivateDirectory -Path $stageRoot } catch { Remove-Item -LiteralPath $stageRoot -Recurse -Force; throw }
    $stagingPath = Join-Path $stageRoot 'bundle'
    New-Item -ItemType Directory -Path $stagingPath | Out-Null
    $archivePath = "$finalPath.zip"
    $archiveCandidate = Join-Path $stageRoot 'bundle.zip'
    $publishedDirectory = $false
    try {
        $publishedKey = Join-Path $stagingPath 'logan-cam.key'
        Copy-Item -LiteralPath $KeyPath -Destination $publishedKey
        Protect-CamPrivateKey -Path $publishedKey
        Render-CamTemplate -TemplatePath (Join-Path $script:CamSetupRoot 'bundle/macos/Install-Logan-MCP.command') -DestinationPath (Join-Path $stagingPath 'Install-Logan-MCP.command') -Values $installerValues
        Copy-Item -LiteralPath (Join-Path $script:CamSetupRoot 'bundle/windows/Double-Click-to-Install.cmd') -Destination (Join-Path $stagingPath 'Double-Click-to-Install.cmd')
        Render-CamTemplate -TemplatePath (Join-Path $script:CamSetupRoot 'bundle/windows/Install-Logan-MCP.ps1') -DestinationPath (Join-Path $stagingPath 'Install-Logan-MCP.ps1') -Values $installerValues
        Render-CamTemplate -TemplatePath (Join-Path $script:CamSetupRoot 'bundle/README.html') -DestinationPath (Join-Path $stagingPath 'README.html') -Values $readmeValues
        New-CamZipArchive -SourceDirectory $stagingPath -ArchivePath $archiveCandidate
        Protect-CamPrivateKey -Path $archiveCandidate
        [void](Assert-CamOutputAvailable -CamId $CamId -OutputDir $OutputDir)
        [IO.Directory]::Move($stagingPath, $finalPath)
        $publishedDirectory = $true
        [IO.File]::Move($archiveCandidate, $archivePath)
        try { Remove-CamStageRootBestEffort -StageRoot $stageRoot }
        catch { Write-Warning "Published outputs are valid, but stage-root cleanup failed: $($_.Exception.Message)" }
        return $finalPath
    }
    catch {
        if (Test-Path -LiteralPath $stageRoot) {
            Remove-Item -LiteralPath $stageRoot -Recurse -Force
        }
        if ($publishedDirectory -and (Test-Path -LiteralPath $finalPath)) {
            Remove-Item -LiteralPath $finalPath -Recurse -Force
        }
        throw
    }
}

function Invoke-CamProvisionRollback {
    param([string]$SshTarget, [string]$CamId, [string]$Fingerprint)

    $json = New-CamDeprovisionJson -CamId $CamId -Fingerprint $Fingerprint
    $result = Invoke-ProvisionSshJson -SshArguments @($SshTarget, 'sudo /opt/logan-mcp/bin/cam-admin deprovision --json') -Json $json
    try { $response = $result.StdOut | ConvertFrom-Json } catch { throw "Rollback returned malformed JSON: $($result.StdOut)" }
    try { $validated = Assert-CamRollbackResponse -Response $response -NativeExitCode $result.ExitCode -CamId $CamId -Fingerprint $Fingerprint } catch { throw "Rollback validation failed: $($result.StdOut)" }
    $validated | Add-Member -NotePropertyName RawResponse -NotePropertyValue $result.StdOut
    return $validated
}

function Assert-CamRollbackResponse {
    param($Response, [int]$NativeExitCode, [string]$CamId, [string]$Fingerprint)
    if ($Response.status -isnot [string] -or $Response.cam_id -isnot [string] -or
        $Response.fingerprint -isnot [string] -or $Response.access_revoked -isnot [bool] -or
        $Response.cam_id -ne $CamId -or $Response.fingerprint -ne $Fingerprint -or $Response.access_revoked -ne $true) {
        throw 'Rollback response did not prove revocation for the exact CAM and fingerprint.'
    }
    if ($Response.status -eq 'SUCCESS' -and $NativeExitCode -eq 0) {
        return [pscustomobject]@{ Revoked = $true; Complete = $true; Status = $Response.status }
    }
    if ($Response.status -eq 'FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED' -and $NativeExitCode -eq 1) {
        return [pscustomobject]@{ Revoked = $true; Complete = $false; Status = $Response.status }
    }
    throw 'Rollback response had an invalid frozen status/exit pair.'
}

function ConvertTo-SanitizedRecoveryText {
    param([AllowEmptyString()][string]$Value)
    $clean = [regex]::Replace($Value, '(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED PRIVATE KEY]')
    return [regex]::Replace($clean, '(?i)private[_ -]?key', '[REDACTED]')
}

function Get-CamManualDeprovisionCommand {
    param([string]$SshTarget, [string]$CamId, [string]$Fingerprint)
    $json = New-CamDeprovisionJson -CamId $CamId -Fingerprint $Fingerprint
    return "'$json' | ssh.exe $SshTarget `"sudo /opt/logan-mcp/bin/cam-admin deprovision --json`""
}

function Write-CamRecoveryMetadata {
    param([string]$OutputDir, [string]$CamId, [string]$SshTarget, [string]$Fingerprint, [string]$ProvisionRequest, [string]$ProvisionResponse, [string]$RollbackResponse)
    $identifier = [guid]::NewGuid().ToString('N')
    $path = Join-Path $OutputDir ("logan-cam-$CamId.recovery.$identifier.json")
    $stageRoot = Join-Path $OutputDir (".logan-cam-$CamId.recovery-stage.$identifier")
    $metadata = [ordered]@{
        cam_id = $CamId
        fingerprint = $Fingerprint
        created_at = [DateTime]::UtcNow.ToString('o')
        manual_command = Get-CamManualDeprovisionCommand -SshTarget $SshTarget -CamId $CamId -Fingerprint $Fingerprint
        provision_request = ConvertTo-SanitizedRecoveryText $ProvisionRequest
        provision_response = ConvertTo-SanitizedRecoveryText $ProvisionResponse
        rollback_response = ConvertTo-SanitizedRecoveryText $RollbackResponse
    } | ConvertTo-Json -Compress
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    try {
        Protect-CamPrivateDirectory -Path $stageRoot
        $candidate = Join-Path $stageRoot 'recovery.json'
        $stream = [IO.File]::Open($candidate, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Dispose()
        Protect-CamPrivateKey -Path $candidate
        Write-Utf8NoBom -Path $candidate -Content $metadata
        if (Test-Path -LiteralPath $path) { throw "Recovery destination unexpectedly exists: $path" }
        [IO.File]::Move($candidate, $path)
        try { Remove-CamStageRootBestEffort -StageRoot $stageRoot } catch { Write-Warning "Recovery metadata was published, but stage cleanup failed: $($_.Exception.Message)" }
        return $path
    }
    catch {
        if (Test-Path -LiteralPath $stageRoot) { Remove-Item -LiteralPath $stageRoot -Recurse -Force }
        throw
    }
}

function Resolve-CamProvisionFailure {
    param([string]$SshTarget, [string]$CamId, [string]$Fingerprint, [string]$OutputDir, [string]$ProvisionRequest, [string]$ProvisionResponse, [string]$Reason)
    $rollbackRaw = ''
    $safeReason = ConvertTo-SanitizedRecoveryText $Reason
    try {
        $rollback = Invoke-CamProvisionRollback -SshTarget $SshTarget -CamId $CamId -Fingerprint $Fingerprint
        $rollbackRaw = $rollback.RawResponse
        if ($rollback.Complete) { throw "Provision did not complete safely; exact-fingerprint rollback succeeded. Original failure: $safeReason" }
        try {
            $recovery = Write-CamRecoveryMetadata -OutputDir $OutputDir -CamId $CamId -SshTarget $SshTarget -Fingerprint $Fingerprint -ProvisionRequest $ProvisionRequest -ProvisionResponse $ProvisionResponse -RollbackResponse $rollbackRaw
        }
        catch { $recovery = ConvertTo-SanitizedRecoveryText ("RECOVERY METADATA WRITE FAILED: $($_.Exception.Message)") }
        throw "Provision did not complete safely; access was revoked but server cleanup remains required. Recovery metadata: $recovery . Original failure: $safeReason"
    }
    catch {
        if ($_.Exception.Message -match '^Provision did not complete safely;') { throw }
        $rollbackRaw = $_.Exception.Message
        $manual = Get-CamManualDeprovisionCommand -SshTarget $SshTarget -CamId $CamId -Fingerprint $Fingerprint
        try {
            $recovery = Write-CamRecoveryMetadata -OutputDir $OutputDir -CamId $CamId -SshTarget $SshTarget -Fingerprint $Fingerprint -ProvisionRequest $ProvisionRequest -ProvisionResponse $ProvisionResponse -RollbackResponse $rollbackRaw
        }
        catch { $recovery = ConvertTo-SanitizedRecoveryText ("RECOVERY METADATA WRITE FAILED: $($_.Exception.Message)") }
        throw "HIGH SEVERITY: revocation was not proven. Run exactly: $manual . Recovery metadata: $recovery"
    }
}

function Get-CamKeyFingerprint {
    param([string]$PublicKeyPath)
    $output = @(& ssh-keygen.exe -lf $PublicKeyPath)
    if ($LASTEXITCODE -ne 0 -or ($output -join ' ') -notmatch '(SHA256:[A-Za-z0-9+/]+)') {
        throw 'ssh-keygen.exe could not calculate the public-key fingerprint.'
    }
    return $matches[1]
}

function New-CamLocalKey {
    param([string]$CamId, [string]$StagingDirectory)
    $privateKeyPath = Join-Path $StagingDirectory 'logan-cam.key'
    & ssh-keygen.exe -q -t ed25519 -f $privateKeyPath -N '""' -C ("logan-cam:$CamId")
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen.exe failed with exit code $LASTEXITCODE." }
    Protect-CamPrivateKey -Path $privateKeyPath
    $publicKeyPath = "$privateKeyPath.pub"
    return [pscustomobject]@{
        PrivateKeyPath = $privateKeyPath
        PublicKey = [IO.File]::ReadAllText($publicKeyPath).Trim()
        Fingerprint = Get-CamKeyFingerprint -PublicKeyPath $publicKeyPath
    }
}

function Invoke-Provision {
    param([string]$CamId, [object[]]$Customers, [object]$AllowDelivery, [string]$SshTarget, [string]$OutputDir)

    $CamId = Normalize-CamId $CamId
    Assert-ValidCamId $CamId
    Assert-ValidCustomers $Customers
    Assert-ValidAllowDelivery $AllowDelivery
    $temporaryDirectory = Assert-CamPreflight -CamId $CamId -OutputDir $OutputDir

    $provisionAttempted = $false
    $json = ''
    $provisionResponse = ''
    try {
        [void](Resolve-CamSshTarget $SshTarget)
        $localKey = New-CamLocalKey -CamId $CamId -StagingDirectory $temporaryDirectory
        $fingerprint = $localKey.Fingerprint
        $json = New-CamProvisionRequest -CamId $CamId -Customers $Customers -AllowDelivery $AllowDelivery -PublicKey $localKey.PublicKey
        try {
            $provisionAttempted = $true
            $result = Invoke-ProvisionSshJson -SshArguments @($SshTarget, 'sudo /opt/logan-mcp/bin/cam-admin provision --json') -Json $json
            $provisionResponse = $result.StdOut
            $response = Read-CamProvisionResponse -StdOut $result.StdOut -NativeExitCode $result.ExitCode
            Assert-CamProvisionResponse -Response $response -NativeExitCode $result.ExitCode -CamId $CamId -Customers $Customers -AllowDelivery $AllowDelivery -Fingerprint $fingerprint
            return Publish-CamBundle -CamId $CamId -OutputDir $OutputDir -KeyPath $localKey.PrivateKeyPath -Connection $response.connection -Fingerprint $response.fingerprint
        }
        catch {
            if ($provisionAttempted) {
                Resolve-CamProvisionFailure -SshTarget $SshTarget -CamId $CamId -Fingerprint $fingerprint -OutputDir $OutputDir -ProvisionRequest $json -ProvisionResponse $provisionResponse -Reason $_.Exception.Message
            }
            throw
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryDirectory) { Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force }
    }
}

function Invoke-ProvisionEntrypoint {
    if (-not $CamId) { $script:CamId = Read-Host 'CAM id' }
    $script:CamId = Normalize-CamId $CamId
    if (-not $Customers) {
        $script:Customers = @((Read-Host 'Customer numbers (comma-separated)').Split(',') | ForEach-Object { [int]$_.Trim() })
    }
    if (-not $script:AllowDeliveryWasSpecified) {
        $script:AllowDelivery = ConvertTo-CamBoolean (Read-Host 'Allow report delivery? (true/false)')
    }
    else {
        $script:AllowDelivery = ConvertTo-CamBoolean $AllowDelivery
    }
    $script:SshTarget = Resolve-CamPromptValue -Value $SshTarget -DefaultValue 'automation1' -PromptText 'Administrator SSH target' -WasSpecified $script:SshTargetWasSpecified
    $defaultOutput = Join-Path $env:USERPROFILE 'logan-cam-bundles'
    $script:OutputDir = Resolve-CamPromptValue -Value $OutputDir -DefaultValue $defaultOutput -PromptText 'Bundle output directory' -WasSpecified $script:OutputDirWasSpecified
    $path = Invoke-Provision -CamId $CamId -Customers $Customers -AllowDelivery $AllowDelivery -SshTarget $SshTarget -OutputDir $OutputDir
    Write-Host "CAM bundle created: $path"
    Write-Warning 'The ZIP file is packaging only; it is not encrypted. Deliver it through a secure channel.'
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ProvisionEntrypoint
}
