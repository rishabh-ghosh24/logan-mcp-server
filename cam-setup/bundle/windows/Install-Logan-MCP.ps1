$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:CamHost = '@@CAM_HOST@@'
$script:CamPort = '@@CAM_PORT@@'
$script:CamRemoteUser = '@@CAM_REMOTE_USER@@'
$script:CamHostPublicKey = '@@CAM_HOST_PUBLIC_KEY@@'

function ConvertTo-TomlString {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function ConvertTo-OpenSshQuotedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($Path -match "[`r`n`\"]") {
        throw "OpenSSH path contains an unsupported character: $Path"
    }
    return '"' + $Path.Replace('\', '/') + '"'
}

function ConvertFrom-TomlTableHeader {
    param([Parameter(Mandatory = $true)][string]$Line)

    $trimmed = $Line.Trim()
    if ($trimmed -match '^\[\[(.*)\]\]\s*(?:#.*)?$') {
        $kind = 'Array'
        $inner = $matches[1]
    }
    elseif ($trimmed -match '^\[(.*)\]\s*(?:#.*)?$') {
        $kind = 'Regular'
        $inner = $matches[1]
    }
    else { return $null }

    $segments = New-Object 'Collections.Generic.List[string]'
    $index = 0
    while ($index -lt $inner.Length) {
        while ($index -lt $inner.Length -and [char]::IsWhiteSpace($inner[$index])) { $index++ }
        if ($index -ge $inner.Length) { return $null }
        if ($inner[$index] -eq '"') {
            $start = $index
            $index++
            $escaped = $false
            $character = $null
            while ($index -lt $inner.Length) {
                $character = $inner[$index]
                $index++
                if ($escaped) { $escaped = $false; continue }
                if ($character -eq '\') { $escaped = $true; continue }
                if ($character -eq '"') { break }
            }
            if ($character -ne '"' -or $escaped) { return $null }
            try { $segment = ($inner.Substring($start, $index - $start) | ConvertFrom-Json) } catch { return $null }
        }
        elseif ($inner[$index] -eq "'") {
            $index++
            $start = $index
            while ($index -lt $inner.Length -and $inner[$index] -ne "'") { $index++ }
            if ($index -ge $inner.Length) { return $null }
            $segment = $inner.Substring($start, $index - $start)
            $index++
        }
        else {
            $start = $index
            while ($index -lt $inner.Length -and $inner[$index] -ne '.') { $index++ }
            $segment = $inner.Substring($start, $index - $start).Trim()
            if ($segment -notmatch '^[A-Za-z0-9_-]+$') { return $null }
        }
        [void]$segments.Add([string]$segment)
        while ($index -lt $inner.Length -and [char]::IsWhiteSpace($inner[$index])) { $index++ }
        if ($index -eq $inner.Length) { break }
        if ($inner[$index] -ne '.') { return $null }
        $index++
    }
    if ($segments.Count -eq 0) { return $null }
    return [pscustomobject]@{ Kind = $kind; Segments = $segments.ToArray() }
}

function Test-IsLoganTableHeader {
    param($Header, [switch]$RootOnly)
    if (-not $Header -or $Header.Segments.Count -lt 2) { return $false }
    if ($Header.Segments[0] -cne 'mcp_servers' -or $Header.Segments[1] -cne 'assurance-logan') { return $false }
    return (-not $RootOnly) -or $Header.Segments.Count -eq 2
}

function Test-SafeTomlStructure {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content)

    if ($Content -match '"""' -or $Content -match "'''") { return $false }
    foreach ($assignmentLine in ($Content -split '\r\n|\n|\r')) {
        $equals = $assignmentLine.IndexOf('=')
        if ($equals -gt 0) {
            $leftHandSide = $assignmentLine.Substring(0, $equals)
            if ($leftHandSide.Contains('\') -and ($leftHandSide.Contains('"') -or $leftHandSide.Contains("'"))) { return $false }
        }
    }
    $mcpKey = '(?:mcp_servers|"mcp_servers"|''mcp_servers'')'
    $loganKey = '(?:assurance-logan|"assurance-logan"|''assurance-logan'')'
    if ($Content -match "(?m)^\s*$mcpKey\s*\.\s*$loganKey\s*=" -or
        $Content -match "(?m)^\s*$mcpKey\s*=\s*\{[^\r\n]*$loganKey\s*=") { return $false }
    $rootLoganTables = 0
    $insideMcpServers = $false
    foreach ($line in ($Content -split '\r\n|\n|\r')) {
        $trimmed = $line.Trim()
        if (-not $trimmed.StartsWith('[')) {
            if ($insideMcpServers -and $trimmed -match "^$loganKey\s*=") { return $false }
            continue
        }
        $header = ConvertFrom-TomlTableHeader $trimmed
        if (-not $header) {
            return $false
        }
        $insideMcpServers = $header.Segments.Count -eq 1 -and $header.Segments[0] -ceq 'mcp_servers'
        if (Test-IsLoganTableHeader $header -RootOnly) {
            $rootLoganTables += 1
            if ($rootLoganTables -gt 1) {
                return $false
            }
        }
    }
    return $true
}

function Assert-SafeExistingInstallerPath {
    param([string]$Path, [string]$Label)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point or symbolic link: $Path"
    }
    if ($item.PSIsContainer -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label must be a regular file when it already exists: $Path"
    }
}

function Assert-InstallerDestinationPaths {
    param([string]$ConfigPath, [string]$KeyPath, [string]$KnownHostsPath)
    Assert-SafeExistingInstallerPath -Path $ConfigPath -Label 'Codex config path'
    Assert-SafeExistingInstallerPath -Path $KeyPath -Label 'Private key path'
    Assert-SafeExistingInstallerPath -Path $KnownHostsPath -Label 'known_hosts path'
}

function Remove-LoganTables {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content)

    if (-not (Test-SafeTomlStructure $Content)) {
        throw 'Refusing to edit config.toml because its table structure is incomplete, unsupported, or ambiguous.'
    }

    $result = New-Object Text.StringBuilder
    $removeCurrentTable = $false
    $lines = [regex]::Split($Content, '(?<=\r\n)|(?<!\r)(?<=\n)|(?<=\r)(?!\n)')
    foreach ($line in $lines) {
        $header = $line.TrimEnd("`r", "`n").Trim()
        if ($header.StartsWith('[')) {
            $parsedHeader = ConvertFrom-TomlTableHeader $header
            $removeCurrentTable = Test-IsLoganTableHeader $parsedHeader
        }
        if (-not $removeCurrentTable) {
            [void]$result.Append($line)
        }
    }
    return $result.ToString()
}

function New-LoganCodexTable {
    param(
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$KnownHostsPath
    )

    $target = '{0}@{1}' -f $script:CamRemoteUser, $script:CamHost
    $arguments = @(
        '-i', $KeyPath,
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', ('UserKnownHostsFile={0}' -f (ConvertTo-OpenSshQuotedPath $KnownHostsPath)),
        '-o', 'ServerAliveInterval=60',
        '-o', 'ServerAliveCountMax=3',
        '-p', ([string]$script:CamPort),
        $target
    )
    $tomlArguments = ($arguments | ForEach-Object { ConvertTo-TomlString ([string]$_) }) -join ', '
    return @"
[mcp_servers."assurance-logan"]
command = "ssh.exe"
args = [$tomlArguments]
"@
}

function Set-LoganCodexConfig {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$KeyPath,
        [Parameter(Mandatory = $true)][string]$KnownHostsPath
    )

    Assert-SafeExistingInstallerPath -Path $ConfigPath -Label 'Codex config path'
    $configExists = Test-Path -LiteralPath $ConfigPath -PathType Leaf
    $original = if ($configExists) { [IO.File]::ReadAllText($ConfigPath).TrimStart([char]0xFEFF) } else { '' }
    if (-not (Test-SafeTomlStructure $original)) {
        throw 'Refusing to edit config.toml because its table structure is incomplete, unsupported, or contains duplicate Logan tables.'
    }

    $cleaned = (Remove-LoganTables $original).TrimEnd("`r", "`n")
    $loganTable = New-LoganCodexTable -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
    $candidateContent = if ($cleaned.Length -eq 0) {
        $loganTable.TrimEnd("`r", "`n") + [Environment]::NewLine
    }
    else {
        $cleaned + [Environment]::NewLine + [Environment]::NewLine +
            $loganTable.TrimEnd("`r", "`n") + [Environment]::NewLine
    }
    if (-not (Test-SafeTomlStructure $candidateContent)) {
        throw 'Generated config.toml failed structural validation.'
    }

    $configDirectory = Split-Path -Parent $ConfigPath
    if (-not (Test-Path -LiteralPath $configDirectory -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
    }
    $backupPath = $null
    if ($configExists) {
        $backupTimestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
        $backupPath = "$ConfigPath.bak.$backupTimestamp"
    }

    $candidatePath = Join-Path $configDirectory ('.{0}.{1}.tmp' -f ([IO.Path]::GetFileName($ConfigPath)), [guid]::NewGuid().ToString('N'))
    $candidateWritten = $false
    try {
        [IO.File]::WriteAllText($candidatePath, $candidateContent, (New-Object Text.UTF8Encoding($false)))
        $candidateWritten = $true
        $written = [IO.File]::ReadAllText($candidatePath)
        if (-not (Test-SafeTomlStructure $written)) {
            throw 'Candidate config.toml failed structural validation after writing.'
        }
        if ($configExists) {
            [IO.File]::Replace($candidatePath, $ConfigPath, $backupPath)
        }
        else {
            Move-Item -LiteralPath $candidatePath -Destination $ConfigPath
        }
        $candidateWritten = $false
    }
    catch {
        if ($candidateWritten -and (Test-Path -LiteralPath $candidatePath)) {
            Remove-Item -LiteralPath $candidatePath -Force
        }
        throw
    }
}

function New-PrivateFileSecurity {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($currentSid)
    $acl.SetAccessRuleProtection($true, $false)
    $systemSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $administratorsSid = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $rules = @(
        [Security.AccessControl.FileSystemAccessRule]::new($currentSid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow),
        [Security.AccessControl.FileSystemAccessRule]::new($systemSid, [Security.AccessControl.FileSystemRights]::Read, [Security.AccessControl.AccessControlType]::Allow),
        [Security.AccessControl.FileSystemAccessRule]::new($administratorsSid, [Security.AccessControl.FileSystemRights]::Read, [Security.AccessControl.AccessControlType]::Allow)
    )
    foreach ($rule in $rules) { [void]$acl.AddAccessRule($rule) }
    return $acl
}

function Set-PrivateFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path)

    Set-Acl -LiteralPath $Path -AclObject (New-PrivateFileSecurity)
    $applied = Get-Acl -LiteralPath $Path
    if ($applied.AreAccessRulesProtected -ne $true -or @($applied.Access).Count -ne 3 -or
        @($applied.Access | Where-Object { $_.IsInherited }).Count -ne 0) {
        throw "Failed to apply the replacement private-file DACL to '$Path'."
    }
}

function New-KnownHostsLine {
    param(
        [Parameter(Mandatory = $true)][string]$HostName,
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$HostPublicKey
    )

    $parts = @($HostPublicKey -split '\s+' | Where-Object { $_ })
    if ($parts.Count -lt 2 -or $parts[0] -notmatch '^ssh-[A-Za-z0-9-]+$' -or
        $parts[1] -notmatch '^[A-Za-z0-9+/]+={0,2}$') {
        throw 'The pinned host public key is not a valid OpenSSH public-key record.'
    }
    $hostToken = if ($Port -eq 22) { $HostName } else { '[{0}]:{1}' -f $HostName, $Port }
    return '{0} {1} {2}' -f $hostToken, $parts[0], $parts[1]
}

function Assert-RenderedInstallerConstants {
    $values = @($script:CamHost, [string]$script:CamPort, $script:CamRemoteUser, $script:CamHostPublicKey)
    $unresolvedTokenPrefix = '@@' + 'CAM_'
    if ($values | Where-Object { $_.Contains($unresolvedTokenPrefix) -or $_ -match "[`r`n]" }) {
        throw 'Installer metadata is incomplete or contains a newline.'
    }
    $port = 0
    if (-not [int]::TryParse([string]$script:CamPort, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        throw 'CAM port must be an integer from 1 through 65535.'
    }
    $script:CamPort = $port
    if ([string]::IsNullOrWhiteSpace($script:CamHost) -or
        [string]::IsNullOrWhiteSpace($script:CamRemoteUser) -or
        [string]::IsNullOrWhiteSpace($script:CamHostPublicKey)) {
        throw 'Installer metadata contains an empty value.'
    }
    if ($script:CamHost -notmatch '^[A-Za-z0-9._:-]+$' -or $script:CamRemoteUser -ne 'cam') {
        throw 'Installer metadata contains an invalid host or restricted account.'
    }
}

function Invoke-Install {
    Assert-RenderedInstallerConstants

    $sourceKeyPath = Join-Path $PSScriptRoot 'logan-cam.key'
    if (-not (Test-Path -LiteralPath $sourceKeyPath -PathType Leaf)) {
        throw "The private key was not found beside the installer: $sourceKeyPath"
    }

    $loganDirectory = Join-Path $env:USERPROFILE '.logan-mcp'
    $keyPath = Join-Path $loganDirectory 'logan-cam.key'
    $knownHostsPath = Join-Path $loganDirectory 'known_hosts'
    $configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
    New-Item -ItemType Directory -Force -Path $loganDirectory | Out-Null
    Assert-InstallerDestinationPaths -ConfigPath $configPath -KeyPath $keyPath -KnownHostsPath $knownHostsPath
    $keyExisted = Test-Path -LiteralPath $keyPath -PathType Leaf
    $knownHostsExisted = Test-Path -LiteralPath $knownHostsPath -PathType Leaf
    $keyBytes = if ($keyExisted) { [IO.File]::ReadAllBytes($keyPath) } else { $null }
    $knownHostsBytes = if ($knownHostsExisted) { [IO.File]::ReadAllBytes($knownHostsPath) } else { $null }
    $keyAcl = if ($keyExisted) { Get-Acl -LiteralPath $keyPath } else { $null }
    $knownHostsAcl = if ($knownHostsExisted) { Get-Acl -LiteralPath $knownHostsPath } else { $null }
    try {
        Copy-Item -LiteralPath $sourceKeyPath -Destination $keyPath -Force
        Set-PrivateFileAcl -Path $keyPath
        $knownHostsLine = New-KnownHostsLine -HostName $script:CamHost -Port $script:CamPort -HostPublicKey $script:CamHostPublicKey
        [IO.File]::WriteAllText($knownHostsPath, $knownHostsLine + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        Set-PrivateFileAcl -Path $knownHostsPath
        Set-LoganCodexConfig -ConfigPath $configPath -KeyPath $keyPath -KnownHostsPath $knownHostsPath
    }
    catch {
        if ($keyExisted) { [IO.File]::WriteAllBytes($keyPath, $keyBytes); Set-Acl -LiteralPath $keyPath -AclObject $keyAcl }
        elseif (Test-Path -LiteralPath $keyPath) { Remove-Item -LiteralPath $keyPath -Force }
        if ($knownHostsExisted) { [IO.File]::WriteAllBytes($knownHostsPath, $knownHostsBytes); Set-Acl -LiteralPath $knownHostsPath -AclObject $knownHostsAcl }
        elseif (Test-Path -LiteralPath $knownHostsPath) { Remove-Item -LiteralPath $knownHostsPath -Force }
        throw
    }

    Write-Host ''
    Write-Host 'Logan MCP was installed. Restart Codex to load the new connection.'
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-Install
}
