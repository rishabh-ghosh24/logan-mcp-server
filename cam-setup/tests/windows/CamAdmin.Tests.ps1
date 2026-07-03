$ErrorActionPreference = 'Stop'

BeforeAll {
    $script:RepositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
    $script:ProvisionScript = Join-Path $RepositoryRoot 'cam-setup/admin/windows/Provision-Logan-CAM.ps1'
    $script:DeprovisionScript = Join-Path $RepositoryRoot 'cam-setup/admin/windows/Deprovision-Logan-CAM.ps1'
    $script:FixtureRoot = Join-Path $RepositoryRoot 'tests/fixtures/cam_admin_contract_v1'
    . $ProvisionScript
    . $DeprovisionScript
}

Describe 'New-CamProvisionRequest' {
    It 'parses only explicit true or false administrator input' {
        ConvertTo-CamBoolean 'true' | Should -BeTrue
        ConvertTo-CamBoolean 'false' | Should -BeFalse
        ConvertTo-CamBoolean $true | Should -BeTrue
        { ConvertTo-CamBoolean 'yes' } | Should -Throw
    }

    It 'emits the exact provision fields and JSON types without private-key material' {
        $publicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGtra2tra2tra2tra2tra2tra2tra2tra2tra2tra2tr logan-cam:cam_alice'
        $json = New-CamProvisionRequest -CamId 'cam_alice' -Customers @(223, 66) -AllowDelivery $false -PublicKey $publicKey
        $payload = $json | ConvertFrom-Json

        @($payload.PSObject.Properties.Name | Sort-Object) -join ',' |
            Should -Be 'allow_delivery,cam_id,customers,public_key'
        $payload.cam_id | Should -Be 'cam_alice'
        @($payload.customers) | Should -Be @(223, 66)
        $payload.allow_delivery -is [bool] | Should -BeTrue
        $payload.public_key | Should -Be $publicKey
        $json | Should -Not -Match 'PRIVATE KEY|KeyPath|private_key'
        $json | Should -Not -Match "[`r`n]"
    }

    It 'rejects invalid CAM ids, customers, and non-boolean delivery values' {
        { New-CamProvisionRequest -CamId '../alice' -Customers @(223) -AllowDelivery $false -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
        { New-CamProvisionRequest -CamId 'cam;id' -Customers @(223) -AllowDelivery $false -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
        { New-CamProvisionRequest -CamId 'cam$(id)' -Customers @(223) -AllowDelivery $false -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
        { Assert-DeprovisionCamId 'cam|id' } | Should -Throw
        { New-CamProvisionRequest -CamId 'cam_alice' -Customers @(0) -AllowDelivery $false -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
        { New-CamProvisionRequest -CamId 'cam_alice' -Customers @('223') -AllowDelivery $false -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
        { New-CamProvisionRequest -CamId 'cam_alice' -Customers @(223) -AllowDelivery 'false' -PublicKey 'ssh-ed25519 AAAA x' } | Should -Throw
    }
}

Describe 'Provision validation and response matching' {
    It 'rejects unsafe SSH targets and existing final output directories' {
        { Assert-SafeSshTarget '-oProxyCommand=bad' } | Should -Throw
        { Assert-SafeSshTarget "admin@host`nother" } | Should -Throw
        $parent = Join-Path $TestDrive 'output'
        New-Item -ItemType Directory -Force -Path (Join-Path $parent 'logan-cam-cam_alice') | Out-Null
        { Assert-CamOutputAvailable -CamId 'cam_alice' -OutputDir $parent } | Should -Throw
    }

    It 'requires exact success identity, policy, and local fingerprint matches' {
        $response = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'provision.response.json') | ConvertFrom-Json
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 0 -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -Fingerprint 'SHA256:sArVyTbDlP2ByuRNM59Xf/iwOUmdy69sxylyNl3iUQQ' } |
            Should -Not -Throw
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 0 -CamId 'cam_bob' -Customers @(223) -AllowDelivery $false -Fingerprint $response.fingerprint } |
            Should -Throw
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 0 -CamId 'cam_alice' -Customers @(223) -AllowDelivery $true -Fingerprint $response.fingerprint } |
            Should -Throw
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 0 -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -Fingerprint 'SHA256:other' } |
            Should -Throw
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 1 -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -Fingerprint $response.fingerprint } | Should -Throw
        $response.customers = @('223')
        { Assert-CamProvisionResponse -Response $response -NativeExitCode 0 -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -Fingerprint $response.fingerprint } | Should -Throw
    }
}

Describe 'Publish-CamBundle' {
    BeforeEach {
        $script:CamSetupRoot = Join-Path $TestDrive 'cam-setup'
        New-Item -ItemType Directory -Force -Path (Join-Path $CamSetupRoot 'bundle/macos') | Out-Null
        New-Item -ItemType Directory -Force -Path (Join-Path $CamSetupRoot 'bundle/windows') | Out-Null
        Set-Content -NoNewline -LiteralPath (Join-Path $CamSetupRoot 'bundle/macos/Install-Logan-MCP.command') -Value 'host=@@CAM_HOST@@ port=@@CAM_PORT@@ user=@@CAM_REMOTE_USER@@ key=@@CAM_HOST_PUBLIC_KEY@@'
        Set-Content -NoNewline -LiteralPath (Join-Path $CamSetupRoot 'bundle/windows/Double-Click-to-Install.cmd') -Value '@echo off'
        Set-Content -NoNewline -LiteralPath (Join-Path $CamSetupRoot 'bundle/windows/Install-Logan-MCP.ps1') -Value 'host=@@CAM_HOST@@ port=@@CAM_PORT@@ user=@@CAM_REMOTE_USER@@ key=@@CAM_HOST_PUBLIC_KEY@@'
        Set-Content -NoNewline -LiteralPath (Join-Path $CamSetupRoot 'bundle/README.html') -Value '<p>@@CAM_ID@@ @@CAM_SERVER_NAME@@ @@CAM_CREATED_AT@@ @@CAM_FINGERPRINT@@</p>'
        $script:KeyPath = Join-Path $TestDrive 'private key'
        Set-Content -NoNewline -LiteralPath $KeyPath -Value 'PRIVATE-KEY-CONTENT'
        $script:Connection = [pscustomobject]@{
            host = 'cam-server.test'
            port = 2222
            remote_user = 'cam'
            host_public_key = 'ssh-ed25519 AAAA host&key'
            server_name = 'CAM <Production>'
        }
    }

    It 'renders the frozen flat Mac and Windows bundle with no remaining tokens' {
        Mock Protect-CamPrivateKey {}
        Mock Protect-CamPrivateDirectory {}
        $output = Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture'

        $output | Should -Be (Join-Path $TestDrive 'logan-cam-cam_alice')
        @(
            'logan-cam.key',
            'Install-Logan-MCP.command',
            'Double-Click-to-Install.cmd',
            'Install-Logan-MCP.ps1',
            'README.html'
        ) | ForEach-Object { Test-Path -LiteralPath (Join-Path $output $_) | Should -BeTrue }
        Test-Path -LiteralPath "$output.zip" | Should -BeTrue
        Get-ChildItem -LiteralPath $output -File | ForEach-Object {
            (Get-Content -Raw -LiteralPath $_.FullName) | Should -Not -Match '@@CAM_'
        }
        (Get-Content -Raw -LiteralPath (Join-Path $output 'README.html')) |
            Should -Match 'CAM &lt;Production&gt;'
        Assert-MockCalled Protect-CamPrivateKey -Times 2 -Exactly
    }

    It 'refuses an existing final directory without changing it' {
        $existing = Join-Path $TestDrive 'logan-cam-cam_alice'
        New-Item -ItemType Directory -Path $existing | Out-Null
        Set-Content -LiteralPath (Join-Path $existing 'sentinel') -Value 'keep'

        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
        Get-Content -Raw -LiteralPath (Join-Path $existing 'sentinel') | Should -Match 'keep'
    }

    It 'refuses an existing archive even when the final directory is absent' {
        Set-Content -LiteralPath (Join-Path $TestDrive 'logan-cam-cam_alice.zip') -Value 'keep'

        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
        Get-Content -Raw -LiteralPath (Join-Path $TestDrive 'logan-cam-cam_alice.zip') | Should -Match 'keep'
    }

    It 'does not move inside or remove a concurrently created final directory' {
        $script:RaceFinal = Join-Path $TestDrive 'logan-cam-cam_alice'
        Mock Protect-CamPrivateKey {}
        Mock Protect-CamPrivateDirectory {}
        Mock New-CamZipArchive {
            param($SourceDirectory, $ArchivePath)
            New-Item -ItemType Directory -Path $script:RaceFinal | Out-Null
            Set-Content -LiteralPath (Join-Path $script:RaceFinal 'concurrent-sentinel') -Value 'keep'
            Set-Content -LiteralPath $ArchivePath -Value 'archive candidate'
        }

        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
        Get-Content -Raw -LiteralPath (Join-Path $script:RaceFinal 'concurrent-sentinel') | Should -Match 'keep'
        Test-Path -LiteralPath (Join-Path $script:RaceFinal 'Install-Logan-MCP.ps1') | Should -BeFalse
    }

    It 'writes Unix executable mode for the Mac installer and safe modes for every ZIP entry' {
        Mock Protect-CamPrivateDirectory {}
        Mock Protect-CamPrivateKey {}
        $output = Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture'
        $metadata = @(Get-CamZipEntryMetadata -ArchivePath "$output.zip")
        @($metadata.Name | Sort-Object) -join ',' | Should -Be 'Double-Click-to-Install.cmd,Install-Logan-MCP.command,Install-Logan-MCP.ps1,README.html,logan-cam.key'
        ($metadata | Where-Object Name -eq 'Install-Logan-MCP.command').UnixMode | Should -Be 33261
        ($metadata | Where-Object Name -eq 'logan-cam.key').UnixMode | Should -Be 33152
        @($metadata | Where-Object { $_.Name -notin @('Install-Logan-MCP.command', 'logan-cam.key') -and $_.UnixMode -ne 33188 }) | Should -HaveCount 0
    }

    It 'keeps both published outputs when late stage-root cleanup fails' {
        Mock Protect-CamPrivateDirectory {}
        Mock Protect-CamPrivateKey {}
        Mock Remove-CamStageRootBestEffort { throw 'forced late cleanup failure' }
        $output = Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $Connection -Fingerprint 'SHA256:fixture' -WarningVariable warnings
        Test-Path -LiteralPath $output | Should -BeTrue
        Test-Path -LiteralPath "$output.zip" | Should -BeTrue
        $warnings -join ' ' | Should -Match 'cleanup'
    }

    It 'rejects template values containing newlines or token delimiters' {
        $badConnection = $Connection.PSObject.Copy()
        $badConnection.host = "bad`nhost"
        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $badConnection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
        $badConnection.host = '@@CAM_HOST@@'
        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $badConnection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
        $badConnection.host = "bad'host"
        { Publish-CamBundle -CamId 'cam_alice' -OutputDir $TestDrive -KeyPath $KeyPath -Connection $badConnection -Fingerprint 'SHA256:fixture' } |
            Should -Throw
    }
}

Describe 'Provision rollback' {
    It 'validates successful and cleanup-required rollback status/exit/type pairs' {
        $success = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.success.response.json') | ConvertFrom-Json
        $cleanup = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.cleanup-required.response.json') | ConvertFrom-Json
        (Assert-CamRollbackResponse -Response $success -NativeExitCode 0 -CamId 'cam_alice' -Fingerprint $success.fingerprint).Complete | Should -BeTrue
        (Assert-CamRollbackResponse -Response $cleanup -NativeExitCode 1 -CamId 'cam_alice' -Fingerprint $cleanup.fingerprint).Complete | Should -BeFalse
        { Assert-CamRollbackResponse -Response $success -NativeExitCode 1 -CamId 'cam_alice' -Fingerprint $success.fingerprint } | Should -Throw
        $success.access_revoked = 'true'
        { Assert-CamRollbackResponse -Response $success -NativeExitCode 0 -CamId 'cam_alice' -Fingerprint $success.fingerprint } | Should -Throw
    }

    It 'writes sanitized recovery metadata when rollback cannot prove revocation' {
        $path = Write-CamRecoveryMetadata -OutputDir $TestDrive -CamId 'cam_alice' -SshTarget 'automation1' -Fingerprint 'SHA256:fixture' -ProvisionRequest '{"public_key":"ssh-ed25519 AAAA"}' -ProvisionResponse '-----BEGIN OPENSSH PRIVATE KEY-----secret-----END OPENSSH PRIVATE KEY-----' -RollbackResponse 'not-json'
        $text = Get-Content -Raw -LiteralPath $path
        $text | Should -Match 'manual_command'
        $text | Should -Match 'cam-admin deprovision --json'
        $text | Should -Not -Match 'secret|BEGIN OPENSSH PRIVATE KEY'
    }

    It 'publishes recovery metadata atomically from a protected stage without clobbering' {
        Mock Protect-CamPrivateDirectory {}
        Mock Protect-CamPrivateKey {}
        $first = Write-CamRecoveryMetadata -OutputDir $TestDrive -CamId 'cam_alice' -SshTarget 'automation1' -Fingerprint 'SHA256:fixture' -ProvisionRequest '{}' -ProvisionResponse '{}' -RollbackResponse '{}'
        $second = Write-CamRecoveryMetadata -OutputDir $TestDrive -CamId 'cam_alice' -SshTarget 'automation1' -Fingerprint 'SHA256:fixture' -ProvisionRequest '{}' -ProvisionResponse '{}' -RollbackResponse '{}'
        $first | Should -Not -Be $second
        Test-Path -LiteralPath $first | Should -BeTrue
        Test-Path -LiteralPath $second | Should -BeTrue
        Get-ChildItem -LiteralPath $TestDrive -Directory -Filter '*.recovery-stage.*' | Should -HaveCount 0
        Assert-MockCalled Protect-CamPrivateDirectory -Times 2 -Exactly
        Assert-MockCalled Protect-CamPrivateKey -Times 2 -Exactly
    }

    It 'retains protected evidence when rollback revoked access but cleanup remains' {
        Mock Invoke-CamProvisionRollback { [pscustomobject]@{ Complete = $false; Revoked = $true; RawResponse = '{"status":"FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED","access_revoked":true}' } }
        Mock Protect-CamPrivateKey {}
        { Resolve-CamProvisionFailure -SshTarget 'automation1' -CamId 'cam_alice' -Fingerprint 'SHA256:fixture' -OutputDir $TestDrive -ProvisionRequest '{"cam_id":"cam_alice"}' -ProvisionResponse '{"status":"SUCCESS"}' -Reason 'publish failed' } | Should -Throw '*cleanup remains*'
        $recovery = @(Get-ChildItem -LiteralPath $TestDrive -Filter '*.recovery.*.json')
        $recovery | Should -HaveCount 1
        (Get-Content -Raw -LiteralPath $recovery[0].FullName) | Should -Match 'FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED'
    }
}

Describe 'Invoke-Provision production path' {
    BeforeEach {
        $script:InvokeStage = Join-Path $TestDrive ('stage-' + [guid]::NewGuid().ToString('N'))
        $script:LocalFingerprint = 'SHA256:sArVyTbDlP2ByuRNM59Xf/iwOUmdy69sxylyNl3iUQQ'
        $script:ValidProvisionJson = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'provision.response.json')
        Mock Assert-CamPreflight { New-Item -ItemType Directory -Path $script:InvokeStage | Out-Null; return $script:InvokeStage }
        Mock Resolve-CamSshTarget { [pscustomobject]@{ Host='host'; User='admin'; Port=22 } }
        Mock New-CamLocalKey { [pscustomobject]@{ PrivateKeyPath=(Join-Path $script:InvokeStage 'logan-cam.key'); PublicKey='ssh-ed25519 AAAA logan-cam:cam_alice'; Fingerprint=$script:LocalFingerprint } }
        Mock Invoke-ProvisionSshJson { [pscustomobject]@{ StdOut=$script:ValidProvisionJson; ExitCode=0 } }
        Mock Publish-CamBundle { Join-Path $TestDrive 'logan-cam-cam_alice' }
    }

    It 'uses only the fixed provision SSH target and command and cleans staging' {
        Invoke-Provision -CamId 'CAM_Alice' -Customers @(223) -AllowDelivery $false -SshTarget 'automation1' -OutputDir $TestDrive | Should -Match 'logan-cam-cam_alice'
        Assert-MockCalled Invoke-ProvisionSshJson -Times 1 -Exactly -ParameterFilter { $SshArguments.Count -eq 2 -and $SshArguments[0] -eq 'automation1' -and $SshArguments[1] -eq 'sudo /opt/logan-mcp/bin/cam-admin provision --json' }
        Test-Path -LiteralPath $script:InvokeStage | Should -BeFalse
    }

    It 'rolls back lost, malformed, and mismatched responses' -ForEach @(
        @{ StdOut=''; ExitCode=255 }
        @{ StdOut='not-json'; ExitCode=0 }
        @{ StdOut='{"status":"SUCCESS","cam_id":"other"}'; ExitCode=0 }
    ) {
        Mock Invoke-ProvisionSshJson { [pscustomobject]@{ StdOut=$StdOut; ExitCode=$ExitCode } }
        Mock Invoke-CamProvisionRollback { [pscustomobject]@{ Complete=$true; Revoked=$true; RawResponse='{"status":"SUCCESS"}' } }
        { Invoke-Provision -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -SshTarget 'automation1' -OutputDir $TestDrive } | Should -Throw '*rollback succeeded*'
        Assert-MockCalled Invoke-CamProvisionRollback -Times 1 -Exactly -ParameterFilter { $CamId -eq 'cam_alice' -and $Fingerprint -eq $script:LocalFingerprint }
        Test-Path -LiteralPath $script:InvokeStage | Should -BeFalse
    }

    It 'rolls back a local publication failure through the real production path' {
        Mock Publish-CamBundle { throw 'disk full' }
        Mock Invoke-CamProvisionRollback { [pscustomobject]@{ Complete=$true; Revoked=$true; RawResponse='{"status":"SUCCESS"}' } }
        { Invoke-Provision -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -SshTarget 'automation1' -OutputDir $TestDrive } | Should -Throw '*rollback succeeded*'
        Assert-MockCalled Invoke-CamProvisionRollback -Times 1 -Exactly
        Test-Path -LiteralPath $script:InvokeStage | Should -BeFalse
    }

    It 'sanitizes recovery metadata when production rollback is unproven' {
        Mock Invoke-ProvisionSshJson { [pscustomobject]@{ StdOut='-----BEGIN OPENSSH PRIVATE KEY-----secret-----END OPENSSH PRIVATE KEY-----'; ExitCode=1 } }
        Mock Invoke-CamProvisionRollback { throw 'rollback not proven' }
        Mock Protect-CamPrivateKey {}
        { Invoke-Provision -CamId 'cam_alice' -Customers @(223) -AllowDelivery $false -SshTarget 'automation1' -OutputDir $TestDrive } | Should -Throw '*HIGH SEVERITY*'
        $recovery = @(Get-ChildItem -LiteralPath $TestDrive -Filter '*.recovery.*.json')
        $recovery | Should -HaveCount 1
        (Get-Content -Raw -LiteralPath $recovery[0].FullName) | Should -Not -Match 'secret|BEGIN OPENSSH PRIVATE KEY'
        Test-Path -LiteralPath $script:InvokeStage | Should -BeFalse
    }
}

Describe 'Deprovision request and status handling' {
    It 'requires show SUCCESS with exit zero and exact JSON field types' {
        $shown = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'show.response.json') | ConvertFrom-Json
        { Assert-CamShowResponse -Response $shown -NativeExitCode 0 -CamId 'cam_alice' } | Should -Not -Throw
        { Assert-CamShowResponse -Response $shown -NativeExitCode 1 -CamId 'cam_alice' } | Should -Throw
        $shown.customers = @('223')
        { Assert-CamShowResponse -Response $shown -NativeExitCode 0 -CamId 'cam_alice' } | Should -Throw
    }

    It 'builds the exact confirmed request' {
        $json = New-CamDeprovisionRequest -CamId 'cam_alice' -Fingerprint 'SHA256:fixture'
        $payload = $json | ConvertFrom-Json

        @($payload.PSObject.Properties.Name | Sort-Object) -join ',' | Should -Be 'cam_id,confirm,expected_fingerprint'
        $payload.cam_id | Should -Be 'cam_alice'
        $payload.confirm | Should -BeTrue
        $payload.expected_fingerprint | Should -Be 'SHA256:fixture'
    }

    It 'treats exit 1 cleanup-required with access_revoked true as revoked but incomplete' {
        $json = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.cleanup-required.response.json')
        $parsed = Read-CamDeprovisionResponse -StdOut $json -NativeExitCode 1

        $parsed.status | Should -Be 'FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED'
        $parsed.access_revoked | Should -BeTrue
        { Assert-CamDeprovisionOutcome -Response $parsed -NativeExitCode 1 } |
            Should -Throw '*access was revoked*cleanup*'
    }

    It 'reports unconfirmed revocation as a high-severity failure' {
        $json = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.unconfirmed.response.json')
        $parsed = Read-CamDeprovisionResponse -StdOut $json -NativeExitCode 1

        { Assert-CamDeprovisionOutcome -Response $parsed -NativeExitCode 1 } |
            Should -Throw '*HIGH SEVERITY*'
    }

    It 'accepts SUCCESS and does not decide from native exit code alone' {
        $json = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.success.response.json')
        $parsed = Read-CamDeprovisionResponse -StdOut $json -NativeExitCode 0
        { Assert-CamDeprovisionOutcome -Response $parsed -NativeExitCode 0 } | Should -Not -Throw
    }

    It 'enforces frozen deprovision status and exit pairs plus exact identity types' {
        $json = Get-Content -Raw -LiteralPath (Join-Path $FixtureRoot 'deprovision.success.response.json')
        $parsed = Read-CamDeprovisionResponse -StdOut $json -NativeExitCode 0
        { Assert-CamDeprovisionOutcome -Response $parsed -NativeExitCode 1 -CamId 'cam_alice' -Fingerprint $parsed.fingerprint } | Should -Throw
        $parsed.access_revoked = 'true'
        { Assert-CamDeprovisionOutcome -Response $parsed -NativeExitCode 0 -CamId 'cam_alice' -Fingerprint $parsed.fingerprint } | Should -Throw
    }
}

Describe 'Preflight, publication race, defaults, and ACL construction' {
    It 'uses prompted defaults on blank input and preserves explicit overrides' {
        Mock Read-Host { '' }
        Resolve-CamPromptValue -Value $null -DefaultValue 'automation1' -PromptText 'SSH target' -WasSpecified $false | Should -Be 'automation1'
        Resolve-CamPromptValue -Value $null -DefaultValue (Join-Path $env:USERPROFILE 'logan-cam-bundles') -PromptText 'Output' -WasSpecified $false | Should -Be (Join-Path $env:USERPROFILE 'logan-cam-bundles')
        Resolve-CamPromptValue -Value 'explicit-host' -DefaultValue 'automation1' -PromptText 'SSH target' -WasSpecified $true | Should -Be 'explicit-host'
        Resolve-DeprovisionPromptValue -Value $null -DefaultValue 'automation1' -PromptText 'SSH target' -WasSpecified $false | Should -Be 'automation1'
        Normalize-CamId 'CAM_Alice' | Should -Be 'cam_alice'
        Assert-MockCalled Read-Host -Times 3 -Exactly
    }

    It 'preflights commands and templates before provisioning and rechecks publication destinations' {
        $source = Get-Content -Raw -LiteralPath $ProvisionScript
        $source | Should -Match 'function Assert-CamPreflight'
        $source | Should -Match "@\('ssh\.exe', 'ssh-keygen\.exe'\)"
        ([regex]::Matches($source, 'Assert-CamOutputAvailable')).Count | Should -BeGreaterThan 2
        $source | Should -Match 'New-Item.*OutputDir'
    }

    It 'protects the publication stage before creating bundle contents or archive data' {
        $source = Get-Content -Raw -LiteralPath $ProvisionScript
        $source.IndexOf('New-Item -ItemType Directory -Path $stageRoot') | Should -BeLessThan $source.IndexOf('Protect-CamPrivateDirectory -Path $stageRoot')
        $source.IndexOf('Protect-CamPrivateDirectory -Path $stageRoot') | Should -BeLessThan $source.IndexOf("Join-Path `$stageRoot 'bundle'")
        $source.IndexOf('New-CamZipArchive') | Should -BeLessThan $source.IndexOf('[IO.Directory]::Move')
    }

    It 'builds a replacement DACL from current user and fixed SIDs' {
        $source = Get-Content -Raw -LiteralPath $ProvisionScript
        $source | Should -Match 'FileSecurity'
        $source | Should -Match 'SetAccessRuleProtection\(\$true, \$false\)'
        $source | Should -Match 'S-1-5-18'
        $source | Should -Match 'S-1-5-32-544'
    }

    It 'replaces a permissive native file DACL with exactly the fixed protected principals' {
        $path = Join-Path $TestDrive 'permissive.key'
        Set-Content -LiteralPath $path -Value 'test'
        $acl = Get-Acl -LiteralPath $path
        $everyone = [Security.Principal.SecurityIdentifier]::new('S-1-1-0')
        $acl.SetAccessRuleProtection($true, $false)
        [void]$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($everyone, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow))
        Set-Acl -LiteralPath $path -AclObject $acl

        Protect-CamPrivateKey -Path $path
        $protected = Get-Acl -LiteralPath $path
        $sids = @($protected.Access | ForEach-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value } | Sort-Object)
        $expected = @(
            [Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
            'S-1-5-18',
            'S-1-5-32-544'
        ) | Sort-Object
        $protected.AreAccessRulesProtected | Should -BeTrue
        @($protected.Access) | Should -HaveCount 3
        $sids -join ',' | Should -Be ($expected -join ',')
        $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $currentRule = $protected.Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $currentSid }
        ([int]$currentRule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::FullControl) | Should -Be ([int][Security.AccessControl.FileSystemRights]::FullControl)
        foreach ($fixedSid in @('S-1-5-18', 'S-1-5-32-544')) {
            $rule = $protected.Access | Where-Object { $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $fixedSid }
            ([int]$rule.FileSystemRights -band [int][Security.AccessControl.FileSystemRights]::Read) | Should -Be ([int][Security.AccessControl.FileSystemRights]::Read)
        }
    }
}

Describe 'Administrator launchers and guarded entrypoints' {
    It 'uses fixed PowerShell launchers and pauses' -ForEach @(
        @{ File = 'Provision-Logan-CAM.cmd'; Script = 'Provision-Logan-CAM.ps1' }
        @{ File = 'Deprovision-Logan-CAM.cmd'; Script = 'Deprovision-Logan-CAM.ps1' }
    ) {
        $path = Join-Path $RepositoryRoot "cam-setup/admin/windows/$File"
        $lines = Get-Content -LiteralPath $path
        $lines | Should -HaveCount 4
        $lines[0] | Should -Be '@echo off'
        $lines[1] | Should -Be "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$Script`""
        $lines[2] | Should -Be 'echo.'
        $lines[3] | Should -Be 'pause'
    }

    It 'guards both PowerShell entrypoints from dot-sourcing' {
        (Get-Content -Raw -LiteralPath $ProvisionScript) | Should -Match "InvocationName -ne '\.'"
        (Get-Content -Raw -LiteralPath $DeprovisionScript) | Should -Match "InvocationName -ne '\.'"
    }
}
