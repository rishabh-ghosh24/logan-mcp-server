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
$RemoteCommandPrefix = "cd /home/opc/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user"

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

function Write-Utf8NoBom {
    param(
        [string]$Path,
        [string]$Value
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Set-PrivateKeyAcl {
    param([string]$KeyPath)

    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $escapedPath = $KeyPath.Replace('"', '\"')
    & cmd.exe /d /c "icacls `"$escapedPath`" /inheritance:r >nul 2>nul"
    & cmd.exe /d /c "icacls `"$escapedPath`" /grant:r `"${user}:(F)`" `"SYSTEM:(R)`" `"Administrators:(R)`" >nul 2>nul"
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

    $pattern = '(?ms)^\[mcp_servers\.logan-mcp\]\r?\n.*?(?=^\[|\z)'
    return [regex]::Replace($ConfigText, $pattern, '').TrimEnd()
}

function Write-CodexConfig {
    param(
        [string]$ConfigPath,
        [string]$KeyPath,
        [string]$LoganUser
    )

    $configDir = Split-Path -Parent $ConfigPath
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null

    $existing = ""
    if (Test-Path -LiteralPath $ConfigPath) {
        $existing = Get-Content -LiteralPath $ConfigPath -Raw
        $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
        Copy-Item -LiteralPath $ConfigPath -Destination "$ConfigPath.backup-$timestamp" -Force
    }

    $remoteCommand = "$RemoteCommandPrefix $LoganUser"
    $args = @(
        "-i",
        $KeyPath,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "$RemoteUser@$VmHost",
        $remoteCommand
    )

    $argsText = ($args | ForEach-Object { ConvertTo-TomlString $_ }) -join ", "
    $loganBlock = @"
[mcp_servers.logan-mcp]
command = "ssh"
args = [$argsText]
"@

    $updated = Remove-ExistingLoganBlock $existing
    if ($updated.Length -gt 0) {
        $updated = $updated + "`r`n`r`n" + $loganBlock + "`r`n"
    } else {
        $updated = $loganBlock + "`r`n"
    }
    Write-Utf8NoBom -Path $ConfigPath -Value $updated
}

function Test-LoganSshConnection {
    param([string]$KeyPath)

    $sshArgs = @(
        "-i",
        $KeyPath,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ServerAliveInterval=60",
        "-o",
        "ServerAliveCountMax=3",
        "$RemoteUser@$VmHost",
        "echo logan-mcp-ok"
    )

    & ssh.exe @sshArgs

    if ($LASTEXITCODE -ne 0) {
        throw "SSH test failed. Check that logan.key is valid and access to $VmHost is allowed."
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
    Write-Host "Codex must be fully restarted before it can use logan-mcp."
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
    Write-Host "Closed Codex. Open Codex App again to use logan-mcp."
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
    if (-not (Test-Path -LiteralPath $KeySourcePath)) {
        throw "Missing SSH key: $KeySourcePath. Place logan.key beside this installer and run it again."
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $keyFileName = "logan-{0}.key" -f (Get-Date -Format "yyyyMMddHHmmssfff")
    $keyPath = Join-Path $InstallDir $keyFileName
    Copy-Item -LiteralPath $KeySourcePath -Destination $keyPath -Force

    if (-not $SkipAcl) {
        Set-PrivateKeyAcl $keyPath
    }
    Remove-OldInstalledKeys -InstallDir $InstallDir -CurrentKeyPath $keyPath

    if (-not $SkipSshTest) {
        Write-Host "Testing SSH connection to logan-mcp VM..."
        Test-LoganSshConnection -KeyPath $keyPath
    }

    Write-CodexConfig `
        -ConfigPath $CodexConfigPath `
        -KeyPath $keyPath `
        -LoganUser $loganUser

    Write-Host ""
    Write-Host "Configured Codex MCP server: logan-mcp"
    Write-Host "Config file: $CodexConfigPath"
    if (-not $SkipCodexRestartPrompt) {
        Confirm-CodexRestart
    } else {
        Write-Host "Restart Codex App for this to take effect."
        Write-Host "If Codex App is not open, just open it normally."
    }
}

Invoke-Install
