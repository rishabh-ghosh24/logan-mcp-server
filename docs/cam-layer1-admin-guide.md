# CAM Layer 1 administrator guide

This guide covers the root-controlled CAM access boundary for Codex App on
macOS and Windows. It does not replace the general `opc`-based development
setup in the main README.

## Responsibility split

The compute administrator performs the one-time server bootstrap, provisions a
dedicated key for each CAM, verifies the assignment, hands off the generated
bundle through an approved secure channel, and deprovisions access when needed.
The administrator also owns recovery and the native/live acceptance gates.

The CAM user does not edit SSH arguments, CAM identity, customer scope, or a
remote command. They only extract their recipient-specific bundle, double-click
the installer for their operating system, and restart Codex once.

## Preconditions

Before changing the server:

- Use the checked-out `access-control` implementation containing the reviewed
  CAM Layer 1 commits.
- Confirm the compute instance principal can access the required OCI Log
  Analytics tenancy resources.
- Take current backups of the existing Logan configuration and state.
- Confirm the administrator can connect through the `automation1` SSH alias.
- Have macOS or Windows OpenSSH tooling available on the administrator machine.
- Select an organization-approved, authenticated, recipient-specific transfer
  channel for bearer credentials.
- Review the source configuration and policy before passing them to bootstrap.

Do not use a real CAM identity or production bundle for acceptance testing.

## One-time server bootstrap

Run from the repository root on the compute after replacing the public host and
confirming each source path:

```bash
sudo cam-setup/server/bootstrap-cam-server.sh \
  --repo "$PWD" \
  --config-source /home/opc/.oci-logan-mcp/config.yaml \
  --policy-source /home/opc/.oci-logan-mcp/access_control.yaml \
  --state-source /home/opc/.oci-logan-mcp \
  --public-host <public-host-or-ip> \
  --port 22 \
  --python python3.11
```

Bootstrap creates timestamped root-owned backups, installs the reviewed runtime
under `/opt/logan-mcp`, installs immutable configuration under
`/etc/logan-mcp`, migrates mutable state to `/home/cam/.oci-logan-mcp`, creates
the restricted `cam` account, installs the SSH forced-command boundary, and
validates the resulting layout. Never use `--root-prefix` outside the repository
test mode.

## Bootstrap verification

Run:

```bash
ssh automation1 'sudo /opt/logan-mcp/bin/cam-admin bootstrap-check --json'
```

Require top-level `status` to be `SUCCESS` and every check below to be `true`:

| JSON check | Meaning |
|---|---|
| `admin_command_root_owned` | The administrative entrypoint cannot be replaced by `cam`. |
| `authorized_keys_immutable` | The managed CAM key file cannot be changed by the restricted account. |
| `cam_not_admin` | The restricted account has no administrative group membership. |
| `cam_password_locked` | Password login is unavailable for `cam`. |
| `config_immutable` | The active Logan configuration cannot be edited by `cam`. |
| `config_parent_immutable` | The configuration cannot be replaced through a writable parent directory. |
| `instance_principal_init` | The restricted runtime can initialize with instance-principal authentication. |
| `launcher_root_owned` | The forced launcher cannot be replaced by `cam`. |
| `policy_immutable` | The CAM access policy cannot be edited by `cam`. |
| `runtime_root_owned` | Runtime code and its virtual environment are not CAM-writable. |
| `runtime_state_writable` | The intended mutable state tree remains usable by Logan. |
| `sshd_effective_config` | Passwords, PTYs, forwarding, tunneling, user RC files, and unsafe environment overrides are disabled. |

Treat a missing or false check as a failed bootstrap. Do not provision a CAM
until it is corrected.

## Provision a CAM from macOS

On the administrator Mac, double-click:

```text
cam-setup/admin/macos/Provision-Logan-CAM.command
```

Enter the CAM id, comma-separated positive customer numbers, delivery policy,
administrator SSH target (default `automation1`), and output directory. Review
the summary and type `YES`. The wrapper generates the key locally and sends
only the public key and policy JSON to the fixed server command.

## Provision a CAM from Windows

On the administrator Windows workstation, double-click:

```text
cam-setup\admin\windows\Provision-Logan-CAM.cmd
```

Supply the same values. Blank SSH target and output prompts use `automation1`
and `%USERPROFILE%\logan-cam-bundles`. The PowerShell workflow applies a
replacement private DACL before publishing the bearer credential.

Both workflows create a directory and a neighboring ZIP:

```text
logan-cam-<cam-id>/
├── logan-cam.key
├── Install-Logan-MCP.command
├── Double-Click-to-Install.cmd
├── Install-Logan-MCP.ps1
└── README.html
```

The ZIP is packaging, not encryption. A successful local bundle is published
only after the server response matches the requested policy and the locally
calculated key fingerprint.

## Secure handoff

- Use an authenticated recipient-specific transfer with access logging.
- Set the transfer to expire in no more than seven days.
- Do not attach the bundle to email, ordinary chat, or a broadly shared folder.
- If the approved transfer channel does not encrypt each transfer, use an
  approved strong encrypted container and send its secret through a separate
  secret channel.
- Verify the recipient before delivery.
- Remove the administrator's local package after the CAM acknowledges
  installation or after seven days, whichever occurs first.
- Treat loss, misdelivery, or unexpected copying as key compromise: deprovision
  immediately, then issue a new key through a new provisioning operation.

## CAM installation

On macOS, extract the bundle and double-click
`Install-Logan-MCP.command`. On Windows, extract it and double-click
`Double-Click-to-Install.cmd`.

The installer copies the key to the user's private Logan directory, pins the
server host key, and replaces only the `logan-mcp` Codex TOML table while
preserving unrelated configuration. Restart Codex once after installation.

The CAM must not edit the generated files to select another identity, customer,
policy, or command. Contact the administrator if installation reports an
ambiguous TOML file, unsafe path, ACL failure, or host-key problem.

## Show and verify without mutation

Use `show` to review the stored assignment and fingerprint:

```bash
ssh automation1 \
  'sudo /opt/logan-mcp/bin/cam-admin show --cam <cam-id> --json'
```

Use `verify` to resolve the current customer entities without changing policy:

```bash
ssh automation1 \
  'sudo /opt/logan-mcp/bin/cam-admin verify --cam <cam-id> --json'
```

Require `SUCCESS`, the expected CAM id and fingerprint, the intended customer
numbers, and only the expected resolved entities. Refresh `show` before
deprovisioning if the fingerprint has changed.

## Deprovision a CAM

From macOS, double-click
`cam-setup/admin/macos/Deprovision-Logan-CAM.command`. From Windows,
double-click `cam-setup\admin\windows\Deprovision-Logan-CAM.cmd`.

The wrapper runs `show`, prints the current customers and fingerprint, and
requires the exact confirmation `REVOKE <cam-id>`. The server then attempts
policy-first blocking, removes the exact forced-command SSH key, terminates the
exact CAM processes, and verifies the resulting live files. If exact identity
or policy cleanup cannot be trusted, it cycles restricted-account sessions;
this can disconnect other CAM sessions, which must reconnect with their valid
keys.

Status meanings:

- `SUCCESS`: policy and key are absent, access is revoked, and cleanup passed.
- `FAILED_ACCESS_REVOKED_CLEANUP_REQUIRED`: access is verified revoked, but
  non-access cleanup remains. Use the printed backup path and failures to
  complete cleanup; do not reissue or restore the old key.
- `FAILED_REVOCATION_UNCONFIRMED`: revocation could not be proved. Treat the key
  as active, follow the exact printed manual command immediately, and escalate.

Ordinary deprovisioning preserves audit history, saved and learned queries,
learning-cron inputs, preferences with continuing value, reports, delivery
metadata, and compliance records. It does not delete the CAM state directory.

## Recovery rules

Provisioning and deprovisioning print protected recovery metadata and exact
manual commands when automatic cleanup cannot be verified. Use the operation's
printed `backup_dir` only to inspect known-good prior files and repair
non-security cleanup deliberately.

Never restore a deprovisioned key or an old `authorized_keys` file merely to
clear an error. Re-authorizing a CAM requires a new authorization decision and
a new provisioning operation with a new key.

After recovery, rerun `bootstrap-check`, `show`, or `verify` as applicable and
record the outcome in the operational change record.

## Legacy shared-key cutover

The old `opc` workflow with a caller-selected `--user` remains only for
administrator or migration testing. Before declaring a CAM production-ready,
remove every legacy shared-key path for that CAM and confirm the delivered
bundle connects only as the restricted `cam` account through the server's
forced command.

## Deferred archive action item

Archival is intentionally outside this release and remains a low-priority,
separately approved operation. The future archive workflow must be explicit,
root-owned, reversible, backup-aware, and manifest/checksum based. It may move
only disposable or inactive artifacts after classifying them.

It must preserve audit history, saved and learned queries needed by the
learning cron, preferences with continuing value, reports and delivery
metadata required for evidence, and compliance records. It must never be an
implicit side effect of deprovisioning.

## Acceptance gates

### Native Windows

Before release, run the native Windows parser and Pester suites on a real
Windows host, exercise the generated installer in a temporary Windows profile,
inspect key and known-host ACLs, verify unrelated Codex TOML survives, and test
idempotence. Extract a Windows-generated ZIP on macOS and confirm
`Install-Logan-MCP.command` is executable and the key is mode `0600`.

```powershell
Invoke-Pester .\cam-setup\tests\windows\CamInstaller.Tests.ps1 -Output Detailed
Invoke-Pester .\cam-setup\tests\windows\CamAdmin.Tests.ps1 -Output Detailed
```

### Live automation1

The live `automation1` acceptance matrix is a separate, explicitly approved
gate. It provisions only a unique temporary CAM, verifies customer scope and
retention, attempts command/environment/PTY/forwarding/SCP/SFTP bypasses,
deprovisions the CAM, proves the old key cannot reconnect, and confirms retained
state remains. Do not combine live mutation approval with the offline native
Windows gate.

Run it only with a separately approved test customer and the exact confirmation:

```bash
./scripts/cam-layer1-live-acceptance.sh \
  --target automation1 \
  --customer <approved-test-customer-number> \
  --confirm-live CAM-LAYER1-LIVE
```
