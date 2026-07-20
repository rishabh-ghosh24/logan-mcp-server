$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$keyPath = Join-Path $PSScriptRoot 'logan.key'
$knownHostsPath = Join-Path $PSScriptRoot 'internal\known_hosts'
$revokeScript = Join-Path $PSScriptRoot 'internal\cam-setup\admin\windows\Deprovision-Logan-CAM.ps1'

foreach ($required in @($keyPath, $knownHostsPath, $revokeScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Administrator toolkit is incomplete: $required"
    }
}

$sshConfig = Join-Path ([IO.Path]::GetTempPath()) ("assurance-logan-admin-{0}.sshconfig" -f [guid]::NewGuid().ToString('N'))
try {
    $keyForSsh = $keyPath.Replace('\', '/')
    $knownHostsForSsh = $knownHostsPath.Replace('\', '/')
    @"
Host automation1
    HostName 130.162.53.112
    User opc
    Port 22
    IdentityFile "$keyForSsh"
    IdentitiesOnly yes
    UserKnownHostsFile "$knownHostsForSsh"
    StrictHostKeyChecking yes
    BatchMode yes
"@ | Set-Content -LiteralPath $sshConfig -Encoding ascii

    & $revokeScript -SshTarget automation1 -SshConfigFile $sshConfig
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
finally {
    Remove-Item -LiteralPath $sshConfig -Force -ErrorAction SilentlyContinue
}
