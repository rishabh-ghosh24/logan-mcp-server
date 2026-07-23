param(
    [string]$UserName,
    [string]$CodexConfigPath,
    [string]$InstallDir,
    [string]$KeySourcePath,
    [switch]$SkipSshTest,
    [switch]$SkipAcl,
    [switch]$SkipCodexRestartPrompt
)

$ErrorActionPreference = "Stop"

$VmHost = "130.162.53.112"
$RemoteUser = "opc"
$VmHostPublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFcj0yHMayP5k838JNY37ZUoyrv79CYtnkBf0BvXsqz1"
$RemoteCommandPrefix = "sudo -n /opt/logan-mcp/bin/admin-launch"

function ConvertTo-LoganUserName {
    param([string]$RawUserName)

    $normalized = $RawUserName.Trim().ToLowerInvariant()
    if ($normalized -notmatch '^[a-z]+\.[a-z]+$') {
        throw "Username must be firstname.lastname using letters only, for example rishabh.ghosh"
    }
    return $normalized
}

function ConvertTo-TomlString {
    param([string]$Value)

    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function ConvertTo-OpenSshQuotedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($Path.Contains('"') -or $Path.Contains("`r") -or $Path.Contains("`n")) {
        throw "OpenSSH path contains an unsupported character: $Path"
    }
    return '"' + $Path.Replace('\', '/') + '"'
}

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Value
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Assert-SafeExistingFile {
    param([string]$Path, [string]$Label)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) {
        return
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a reparse point or symbolic link: $Path"
    }
    if ($item.PSIsContainer -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label must be a regular file: $Path"
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
    foreach ($rule in $rules) {
        [void]$acl.AddAccessRule($rule)
    }
    return $acl
}

function Set-PrivateFileAcl {
    param([string]$Path)

    Set-Acl -LiteralPath $Path -AclObject (New-PrivateFileSecurity)
    $applied = Get-Acl -LiteralPath $Path
    if (-not $applied.AreAccessRulesProtected -or @($applied.Access).Count -ne 3 -or
        @($applied.Access | Where-Object { $_.IsInherited }).Count -ne 0) {
        throw "Failed to apply the private-file ACL to '$Path'."
    }
}

function Remove-OldInstalledKeys {
    param(
        [string]$InstallDir,
        [string]$CurrentKeyPath
    )

    Get-ChildItem -LiteralPath $InstallDir -Filter "logan*.key" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $CurrentKeyPath } |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Remove-ExistingLoganBlock {
    param([string]$ConfigText)

    $result = New-Object Text.StringBuilder
    $skip = $false
    $insideMcpServers = $false
    $lines = [regex]::Split($ConfigText, '(?<=\r\n)|(?<!\r)(?<=\n)|(?<=\r)(?!\n)')
    foreach ($line in $lines) {
        if ($line.Contains('"""') -or $line.Contains("'''")) {
            throw 'Multiline TOML strings are unsupported; config.toml was not changed.'
        }
        $trimmed = $line.Trim()
        $compact = ($trimmed -replace '\s', '').Replace('"', '').Replace("'", '')
        if ($trimmed.StartsWith('[')) {
            $insideMcpServers = $compact -eq '[mcp_servers]'
            $skip = $compact -match '^\[\[?mcp_servers\.(?:logan-mcp|assurance-logan)(?:\.|\]|$)'
        }
        elseif (-not $skip -and $insideMcpServers -and
            $compact -match '^(?:logan-mcp|assurance-logan)=') {
            throw 'Unsupported inline Logan MCP declaration in config.toml.'
        }
        elseif (-not $skip -and $compact -match '^mcp_servers\.(?:logan-mcp|assurance-logan)=') {
            throw 'Unsupported dotted Logan MCP declaration in config.toml.'
        }
        if (-not $skip) {
            [void]$result.Append($line)
        }
    }
    return $result.ToString().TrimEnd()
}

function Write-CodexConfig {
    param(
        [string]$ConfigPath,
        [string]$KeyPath,
        [string]$KnownHostsPath,
        [string]$LoganUser
    )

    $configDir = Split-Path -Parent $ConfigPath
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null

    $existing = ""
    if (Test-Path -LiteralPath $ConfigPath) {
        $existing = Get-Content -LiteralPath $ConfigPath -Raw
    }

    $remoteCommand = "$RemoteCommandPrefix $LoganUser"
    $args = @(
        "-T",
        "-i",
        $KeyPath,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=$(ConvertTo-OpenSshQuotedPath $KnownHostsPath)",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "$RemoteUser@$VmHost",
        $remoteCommand
    )

    $argsText = ($args | ForEach-Object { ConvertTo-TomlString $_ }) -join ", "
    $loganBlock = @"
[mcp_servers.assurance-logan]
command = "ssh.exe"
args = [$argsText]
"@

    $updated = Remove-ExistingLoganBlock $existing
    if ($updated.Length -gt 0) {
        $updated = $updated + "`r`n`r`n" + $loganBlock + "`r`n"
    } else {
        $updated = $loganBlock + "`r`n"
    }
    $candidatePath = Join-Path $configDir ('.{0}.{1}.tmp' -f ([IO.Path]::GetFileName($ConfigPath)), [guid]::NewGuid().ToString('N'))
    try {
        Write-Utf8NoBom -Path $candidatePath -Value $updated
        if (Test-Path -LiteralPath $ConfigPath) {
            $timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')
            $backupPath = "$ConfigPath.backup-$timestamp"
            [IO.File]::Replace($candidatePath, $ConfigPath, $backupPath)
        }
        else {
            Move-Item -LiteralPath $candidatePath -Destination $ConfigPath
        }
    }
    catch {
        if (Test-Path -LiteralPath $candidatePath) {
            Remove-Item -LiteralPath $candidatePath -Force
        }
        throw
    }
}

function Test-LoganSshConnection {
    param([string]$KeyPath, [string]$KnownHostsPath)

    $sshArgs = @(
        "-T",
        "-i",
        $KeyPath,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=$(ConvertTo-OpenSshQuotedPath $KnownHostsPath)",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "$RemoteUser@$VmHost",
        "sudo -n test -x /opt/logan-mcp/bin/admin-launch && echo assurance-logan-ok"
    )

    & ssh.exe @sshArgs

    if ($LASTEXITCODE -ne 0) {
        throw "SSH or admin-launch test failed. Check logan.key and access to $VmHost."
    }
}

function Get-CodexProcesses {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.ProcessName -eq "Codex" -or $_.ProcessName -eq "codex") -and
            ($_.Path -like "*\OpenAI\Codex\*" -or $_.Path -like "*\OpenAI.Codex_*")
        }
}

function Confirm-CodexRestart {
    $codexProcesses = @(Get-CodexProcesses)
    if ($codexProcesses.Count -eq 0) {
        Write-Host "Codex App is not running. Open it normally when you are ready."
        return
    }

    Write-Host ""
    Write-Host "Codex must be fully restarted before it can use assurance-logan."
    Write-Host "This will close running Codex windows and background Codex processes."
    Write-Host "Save or finish any active Codex work before continuing."
    $answer = Read-Host "Press Enter to close Codex now, or type S and press Enter to skip"
    if ($answer.Trim().ToLowerInvariant() -eq "s") {
        Write-Host "Skipped closing Codex. Close it completely from Task Manager, then open it again."
        return
    }

    foreach ($process in $codexProcesses) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Closed Codex. Open Codex App again to use assurance-logan."
}

function Invoke-Install {
    $scriptDir = Split-Path -Parent $PSCommandPath
    if (-not $InstallDir) {
        $InstallDir = Join-Path $HOME ".logan-mcp"
    }
    if (-not $CodexConfigPath) {
        $CodexConfigPath = Join-Path $HOME ".codex\config.toml"
    }
    if (-not $KeySourcePath) {
        $KeySourcePath = Join-Path $scriptDir "logan.key"
    }
    if (-not $UserName) {
        $UserName = Read-Host "Enter Logan username in firstname.lastname format"
    }

    $loganUser = ConvertTo-LoganUserName $UserName
    if (-not (Test-Path -LiteralPath $KeySourcePath -PathType Leaf)) {
        throw "Missing SSH key: $KeySourcePath. Place logan.key beside this installer and run it again."
    }
    Assert-SafeExistingFile -Path $KeySourcePath -Label 'Installer private key'

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $keyFileName = "logan-{0}.key" -f (Get-Date -Format "yyyyMMddHHmmssfff")
    $keyPath = Join-Path $InstallDir $keyFileName
    $knownHostsPath = Join-Path $InstallDir "known_hosts"
    Assert-SafeExistingFile -Path $knownHostsPath -Label 'known_hosts path'
    Assert-SafeExistingFile -Path $CodexConfigPath -Label 'Codex config path'
    Copy-Item -LiteralPath $KeySourcePath -Destination $keyPath -Force
    Write-Utf8NoBom -Path $knownHostsPath -Value "$VmHost $VmHostPublicKey`r`n"

    if (-not $SkipAcl) {
        Set-PrivateFileAcl $keyPath
        Set-PrivateFileAcl $knownHostsPath
    }
    if (-not $SkipSshTest) {
        Write-Host "Testing SSH connection to the assurance-logan runtime..."
        Test-LoganSshConnection -KeyPath $keyPath -KnownHostsPath $knownHostsPath
    }

    Write-CodexConfig `
        -ConfigPath $CodexConfigPath `
        -KeyPath $keyPath `
        -KnownHostsPath $knownHostsPath `
        -LoganUser $loganUser
    Remove-OldInstalledKeys -InstallDir $InstallDir -CurrentKeyPath $keyPath

    Write-Host ""
    Write-Host "Configured Codex MCP server: assurance-logan"
    Write-Host "Config file: $CodexConfigPath"
    if (-not $SkipCodexRestartPrompt) {
        Confirm-CodexRestart
    } else {
        Write-Host "Restart Codex App for this to take effect."
        Write-Host "If Codex App is not open, just open it normally."
    }
}

Invoke-Install
