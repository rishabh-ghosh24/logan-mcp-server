$ErrorActionPreference = 'Stop'

BeforeAll {
    $script:RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $script:InstallerPath = Join-Path $RepositoryRoot 'cam-setup/bundle/windows/Install-Logan-MCP.ps1'
    $script:LauncherPath = Join-Path $RepositoryRoot 'cam-setup/bundle/windows/Double-Click-to-Install.cmd'
    . $InstallerPath
}

Describe 'ConvertTo-TomlString' {
    It 'quotes and escapes Windows paths and quotes' {
        ConvertTo-TomlString 'C:\Users\A User\"key"' |
            Should -Be '"C:\\Users\\A User\\\"key\""'
    }
}

Describe 'ConvertTo-OpenSshQuotedPath' {
    It 'quotes and normalizes a Windows profile path containing spaces' {
        ConvertTo-OpenSshQuotedPath 'C:\Users\Iustin Dorila\.logan-mcp\known_hosts' |
            Should -Be '"C:/Users/Iustin Dorila/.logan-mcp/known_hosts"'
    }

    It 'rejects characters that cannot be safely embedded in an OpenSSH option' {
        { ConvertTo-OpenSshQuotedPath "C:\Users\bad`nname\known_hosts" } | Should -Throw
        { ConvertTo-OpenSshQuotedPath 'C:\Users\bad"name\known_hosts' } | Should -Throw
    }
}

Describe 'Logan TOML table safety' {
    It 'accepts supported fixture <Name>' -ForEach @(
        @{ Name = 'empty'; Content = '' }
        @{ Name = 'unrelated'; Content = "[mcp_servers.other]`ncommand = `"other`"`n" }
        @{ Name = 'bare Logan'; Content = "[mcp_servers.assurance-logan]`ncommand = `"old`"`n" }
        @{ Name = 'quoted Logan'; Content = "[mcp_servers.`"assurance-logan`"]`ncommand = `"old`"`n" }
        @{ Name = 'nested Logan'; Content = "[mcp_servers.`"assurance-logan`".env]`nSAFE = `"yes`"`n" }
        @{ Name = 'quoted project path'; Content = "[projects.`"C:\\Work Folder`"] # retained`ntrust_level = `"trusted`"`n" }
        @{ Name = 'unrelated array table'; Content = "[[agents]]`nname = `"keep`"`n" }
        @{ Name = 'fully quoted Logan'; Content = "[`"mcp_servers`".`"assurance-logan`"]`ncommand = `"old`"`n" }
    ) {
        Test-SafeTomlStructure $Content | Should -BeTrue
    }

    It 'rejects unsafe fixture <Name>' -ForEach @(
        @{ Name = 'incomplete'; Content = "[mcp_servers.other`ncommand = `"other`"`n" }
        @{ Name = 'mismatched array table'; Content = "[[mcp_servers.other]`ncommand = `"other`"`n" }
        @{ Name = 'trailing text'; Content = "[mcp_servers.other] garbage`n" }
        @{ Name = 'duplicate aliases'; Content = "[mcp_servers.assurance-logan]`na = 1`n[mcp_servers.`"assurance-logan`"]`nb = 2`n" }
        @{ Name = 'duplicate quoted roots'; Content = "[mcp_servers.`"assurance-logan`"]`na = 1`n[mcp_servers.`"assurance-logan`"]`nb = 2`n" }
        @{ Name = 'top-level dotted alias'; Content = "mcp_servers.assurance-logan = { command = `"bad`" }`n" }
        @{ Name = 'top-level quoted alias'; Content = "`"mcp_servers`".`"assurance-logan`" = { command = `"bad`" }`n" }
        @{ Name = 'parent table alias'; Content = "[mcp_servers]`n`"assurance-logan`" = { command = `"bad`" }`n" }
        @{ Name = 'inline parent alias'; Content = "mcp_servers = { assurance-logan = { command = `"bad`" } }`n" }
        @{ Name = 'multiline basic string'; Content = "note = `"`"`"[mcp_servers.assurance-logan]`nnot a table`"`"`"`n" }
        @{ Name = 'escaped quoted alias'; Content = "`"mcp\u005fservers`".`"assurance-logan`" = { command = `"bad`" }`n" }
    ) {
        Test-SafeTomlStructure $Content | Should -BeFalse
    }

    It 'removes root Logan tables and their nested tables only' {
        $input = @'
title = "keep"
[mcp_servers.other]
command = "other"
[mcp_servers."assurance-logan"]
command = "old"
[mcp_servers."assurance-logan".env]
SECRET = "remove"
[projects."C:\Work Folder"]
trust_level = "trusted"
'@

        $result = Remove-LoganTables $input

        $result | Should -Match 'title = "keep"'
        $result | Should -Match '\[mcp_servers\.other\]'
        $result | Should -Match '\[projects\."C:\\Work Folder"\]'
        $result | Should -Not -Match 'assurance-logan'
        $result | Should -Not -Match 'SECRET'
    }

    It 'removes semantic quoted and array Logan tables while preserving unrelated arrays' {
        $input = "[[agents]]`nname = `"keep`"`n[`"mcp_servers`".`"assurance-logan`"]`ncommand = `"old`"`n[[`"mcp_servers`".`"assurance-logan`".env]]`nSECRET = `"remove`"`n"
        $result = Remove-LoganTables $input
        $result | Should -Match '\[\[agents\]\]'
        $result | Should -Not -Match 'assurance-logan|SECRET'
    }

    It 'counts semantically equivalent quoted roots as duplicates' {
        $content = "[mcp_servers.assurance-logan]`na=1`n[`"mcp_servers`".`"assurance-logan`"]`nb=2`n"
        Test-SafeTomlStructure $content | Should -BeFalse
    }
}

Describe 'Set-LoganCodexConfig' {
    BeforeEach {
        $script:CamHost = 'cam.example.test'
        $script:CamPort = 2222
        $script:CamRemoteUser = 'cam'
        $script:TestRoot = Join-Path $TestDrive 'Profile With Spaces'
        $script:ConfigPath = Join-Path $TestRoot '.codex/config.toml'
        $script:KeyPath = Join-Path $TestRoot '.logan-mcp/logan-cam.key'
        $script:KnownHostsPath = Join-Path $TestRoot '.logan-mcp/known_hosts'
        New-Item -ItemType Directory -Force -Path (Split-Path $ConfigPath) | Out-Null
    }

    It 'writes fixed pinned ssh.exe arguments and preserves unrelated tables' {
        Set-Content -LiteralPath $ConfigPath -Value "[mcp_servers.other]`ncommand = `"other`"`n[mcp_servers.logan-mcp]`ncommand = `"regular`"`n" -NoNewline

        Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
        $content = [IO.File]::ReadAllText($ConfigPath)

        $content | Should -Match '\[mcp_servers\."assurance-logan"\]'
        $content | Should -Match 'command = "ssh\.exe"'
        $content | Should -Match [regex]::Escape((ConvertTo-TomlString $KeyPath))
        $content | Should -Match 'BatchMode=yes'
        $content | Should -Match 'IdentitiesOnly=yes'
        $content | Should -Match 'StrictHostKeyChecking=yes'
        $content | Should -Match 'UserKnownHostsFile=\\".*Profile With Spaces.*known_hosts\\"'
        $content | Should -Match 'ServerAliveInterval=60'
        $content | Should -Match 'ServerAliveCountMax=3'
        $content | Should -Match '"-p", "2222", "cam@cam\.example\.test"'
        $content | Should -Match '\[mcp_servers\.other\]'
        $content | Should -Match '\[mcp_servers\.logan-mcp\]'
        $content | Should -Match 'command = "regular"'
    }

    It 'is byte-idempotent and creates a backup of the previous valid config' {
        [IO.File]::WriteAllText($ConfigPath, "title = `"keep`"`n", [Text.UTF8Encoding]::new($false))
        Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
        $first = [IO.File]::ReadAllBytes($ConfigPath)

        Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
        $second = [IO.File]::ReadAllBytes($ConfigPath)

        [Convert]::ToBase64String($second) | Should -Be ([Convert]::ToBase64String($first))
        @(Get-ChildItem -LiteralPath (Split-Path $ConfigPath) -Filter 'config.toml.bak.*') | Should -HaveCount 2
    }

    It 'writes UTF-8 without a BOM' {
        [IO.File]::WriteAllText($ConfigPath, "title = `"keep`"`n", (New-Object Text.UTF8Encoding($true)))
        Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
        $bytes = [IO.File]::ReadAllBytes($ConfigPath)

        $bytes.Length | Should -BeGreaterThan 3
        @($bytes[0], $bytes[1], $bytes[2]) -join ',' | Should -Not -Be '239,187,191'
    }

    It 'leaves malformed or duplicate originals byte-identical' -ForEach @(
        @{ Content = "[broken`nvalue = 1`n" }
        @{ Content = "[mcp_servers.assurance-logan]`na = 1`n[mcp_servers.`"assurance-logan`"]`nb = 2`n" }
        @{ Content = "mcp_servers.assurance-logan = { command = `"bad`" }`n" }
        @{ Content = "`"mcp_servers`".`"assurance-logan`" = { command = `"bad`" }`n" }
        @{ Content = "[mcp_servers]`nassurance-logan = { command = `"bad`" }`n" }
        @{ Content = "mcp_servers = { assurance-logan = { command = `"bad`" } }`n" }
        @{ Content = "note = `"`"`"[mcp_servers.assurance-logan]`nnot a table`"`"`"`n" }
        @{ Content = "`"mcp\u005fservers`".`"assurance-logan`" = { command = `"bad`" }`n" }
    ) {
        [IO.File]::WriteAllBytes($ConfigPath, [Text.Encoding]::UTF8.GetBytes($Content))
        $before = [Convert]::ToBase64String([IO.File]::ReadAllBytes($ConfigPath))

        { Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath } |
            Should -Throw

        [Convert]::ToBase64String([IO.File]::ReadAllBytes($ConfigPath)) | Should -Be $before
    }

    It 'does not expose identity policy or a remote-command surface' {
        Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath
        $content = [IO.File]::ReadAllText($ConfigPath)

        $content | Should -Not -Match '--user|--enforce-access|OCI_LOGAN_MCP|LOGAN_USER|remote.command'
        ([regex]::Matches($content, 'cam@cam\.example\.test')).Count | Should -Be 1
        $content.TrimEnd() | Should -Match '"cam@cam\.example\.test"\]\s*$'
    }

    It 'rejects a config path that is a directory without touching its contents' {
        Remove-Item -LiteralPath $ConfigPath -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path $ConfigPath | Out-Null
        $sentinel = Join-Path $ConfigPath 'sentinel'
        Set-Content -NoNewline -LiteralPath $sentinel -Value 'keep'
        { Set-LoganCodexConfig -ConfigPath $ConfigPath -KeyPath $KeyPath -KnownHostsPath $KnownHostsPath } | Should -Throw '*regular file*'
        Get-Content -Raw -LiteralPath $sentinel | Should -Be 'keep'
        @(Get-ChildItem -LiteralPath $ConfigPath) | Should -HaveCount 1
    }
}

Describe 'Private key and known_hosts installation' {
    It 'constructs a protected replacement DACL with only the three fixed principals' {
        $source = [IO.File]::ReadAllText($InstallerPath)

        $source | Should -Match 'FileSecurity'
        $source | Should -Match 'SetAccessRuleProtection\(\$true, \$false\)'
        $source | Should -Match 'S-1-5-18'
        $source | Should -Match 'S-1-5-32-544'
        $source | Should -Match 'FullControl'
        $source | Should -Match 'Set-Acl'
        $source | Should -Match 'Set-PrivateFileAcl -Path \$keyPath'
        $source | Should -Match 'Set-PrivateFileAcl -Path \$knownHostsPath'
    }

    It 'replaces a permissive native DACL with only the current user and fixed read principals' {
        $path = Join-Path $TestDrive 'permissive-installer-key'
        Set-Content -LiteralPath $path -Value 'test key'
        $permissive = Get-Acl -LiteralPath $path
        $everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
        $permissive.SetAccessRuleProtection($true, $false)
        [void]$permissive.AddAccessRule(
            [Security.AccessControl.FileSystemAccessRule]::new(
                $everyone,
                [Security.AccessControl.FileSystemRights]::FullControl,
                [Security.AccessControl.AccessControlType]::Allow
            )
        )
        Set-Acl -LiteralPath $path -AclObject $permissive

        Set-PrivateFileAcl -Path $path

        $protected = Get-Acl -LiteralPath $path
        $rules = @($protected.Access)
        $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $expectedSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544') | Sort-Object
        $actualSids = @($rules | ForEach-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        } | Sort-Object)

        $protected.AreAccessRulesProtected | Should -BeTrue
        @($rules | Where-Object { $_.IsInherited }) | Should -HaveCount 0
        $rules | Should -HaveCount 3
        $actualSids -join ',' | Should -Be ($expectedSids -join ',')
        $actualSids | Should -Not -Contain 'S-1-1-0'

        $currentRule = $rules | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $currentSid
        }
        ([int]$currentRule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::FullControl) |
            Should -Be ([int][Security.AccessControl.FileSystemRights]::FullControl)

        foreach ($fixedSid in @('S-1-5-18', 'S-1-5-32-544')) {
            $rule = $rules | Where-Object {
                $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $fixedSid
            }
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Read) |
                Should -Be ([int][Security.AccessControl.FileSystemRights]::Read)
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Write) |
                Should -Be 0
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Delete) |
                Should -Be 0
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::ChangePermissions) |
                Should -Be 0
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::TakeOwnership) |
                Should -Be 0
        }
    }

    It 'pins a port-22 host without brackets and a non-22 host with brackets' {
        New-KnownHostsLine -HostName 'cam.example.test' -Port 22 -HostPublicKey 'ssh-ed25519 AAAA fixture' |
            Should -Be 'cam.example.test ssh-ed25519 AAAA'
        New-KnownHostsLine -HostName 'cam.example.test' -Port 2222 -HostPublicKey 'ssh-ed25519 AAAA fixture' |
            Should -Be '[cam.example.test]:2222 ssh-ed25519 AAAA'
    }
}

Describe 'Invoke-Install transaction rollback' {
    It 'restores existing key host pin ACLs and config bytes when config publication fails' {
        $bundle = Join-Path $TestDrive 'Rendered Bundle'
        $profile = Join-Path $TestDrive 'Existing Profile'
        New-Item -ItemType Directory -Path $bundle, (Join-Path $profile '.logan-mcp'), (Join-Path $profile '.codex') -Force | Out-Null
        $rendered = [IO.File]::ReadAllText($InstallerPath).Replace('@@CAM_HOST@@', 'new.example').Replace('@@CAM_PORT@@', '22').Replace('@@CAM_REMOTE_USER@@', 'cam').Replace('@@CAM_HOST_PUBLIC_KEY@@', 'ssh-ed25519 AAAA new')
        $renderedPath = Join-Path $bundle 'Install-Logan-MCP.ps1'
        [IO.File]::WriteAllText($renderedPath, $rendered, [Text.UTF8Encoding]::new($false))
        Set-Content -NoNewline -LiteralPath (Join-Path $bundle 'logan-cam.key') -Value 'NEW-KEY'
        $key = Join-Path $profile '.logan-mcp/logan-cam.key'
        $known = Join-Path $profile '.logan-mcp/known_hosts'
        $config = Join-Path $profile '.codex/config.toml'
        [IO.File]::WriteAllBytes($key, [Text.Encoding]::UTF8.GetBytes('OLD-KEY'))
        [IO.File]::WriteAllBytes($known, [Text.Encoding]::UTF8.GetBytes('old.example ssh-ed25519 AAAA old'))
        [IO.File]::WriteAllBytes($config, [Text.Encoding]::UTF8.GetBytes("model = `"keep`"`n"))
        Set-PrivateFileAcl -Path $key
        Set-PrivateFileAcl -Path $known
        $keyAcl = (Get-Acl -LiteralPath $key).Sddl
        $knownAcl = (Get-Acl -LiteralPath $known).Sddl
        $beforeKey = [Convert]::ToBase64String([IO.File]::ReadAllBytes($key))
        $beforeKnown = [Convert]::ToBase64String([IO.File]::ReadAllBytes($known))
        $beforeConfig = [Convert]::ToBase64String([IO.File]::ReadAllBytes($config))
        $oldProfile = $env:USERPROFILE
        try {
            $env:USERPROFILE = $profile
            . $renderedPath
            Mock Set-LoganCodexConfig { throw 'forced config failure' }
            { Invoke-Install } | Should -Throw '*forced config failure*'
        }
        finally { $env:USERPROFILE = $oldProfile }
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($key)) | Should -Be $beforeKey
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($known)) | Should -Be $beforeKnown
        [Convert]::ToBase64String([IO.File]::ReadAllBytes($config)) | Should -Be $beforeConfig
        (Get-Acl -LiteralPath $key).Sddl | Should -Be $keyAcl
        (Get-Acl -LiteralPath $known).Sddl | Should -Be $knownAcl
    }

    It 'rejects credential directories and reparse points without touching their targets' {
        $root = Join-Path $TestDrive 'unsafe destinations'
        $keyDirectory = Join-Path $root 'key-directory'
        $junctionTarget = Join-Path $root 'junction-target'
        $knownJunction = Join-Path $root 'known-junction'
        New-Item -ItemType Directory -Path $keyDirectory, $junctionTarget -Force | Out-Null
        Set-Content -NoNewline -LiteralPath (Join-Path $keyDirectory 'sentinel') -Value 'key-keep'
        Set-Content -NoNewline -LiteralPath (Join-Path $junctionTarget 'sentinel') -Value 'known-keep'
        New-Item -ItemType Junction -Path $knownJunction -Target $junctionTarget | Out-Null

        { Assert-InstallerDestinationPaths -ConfigPath (Join-Path $root 'config.toml') -KeyPath $keyDirectory -KnownHostsPath (Join-Path $root 'known_hosts') } |
            Should -Throw '*regular file*'
        { Assert-InstallerDestinationPaths -ConfigPath (Join-Path $root 'config.toml') -KeyPath (Join-Path $root 'key') -KnownHostsPath $knownJunction } |
            Should -Throw '*reparse point*'

        Get-Content -Raw -LiteralPath (Join-Path $keyDirectory 'sentinel') | Should -Be 'key-keep'
        Get-Content -Raw -LiteralPath (Join-Path $junctionTarget 'sentinel') | Should -Be 'known-keep'
        Test-Path -LiteralPath $knownJunction | Should -BeTrue
    }
}

Describe 'Double-click launcher' {
    It 'contains only the fixed PowerShell launcher, spacer, and pause' {
        $lines = Get-Content -LiteralPath $LauncherPath
        $lines | Should -HaveCount 4
        $lines[0] | Should -Be '@echo off'
        $lines[1] | Should -Be 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Logan-MCP.ps1"'
        $lines[2] | Should -Be 'echo.'
        $lines[3] | Should -Be 'pause'
    }
}
