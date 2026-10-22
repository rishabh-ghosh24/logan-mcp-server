# Security policy

## Supported version

Security fixes target the latest code on `main`. Include your installed package version and Git revision when reporting an issue.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/rishabh-ghosh24/logan-mcp-server/security/advisories/new). Private reporting is enabled for this repository.

Include a description, affected version, reproduction steps, likely impact and a minimal example with credentials and customer data removed. Follow [GitHub's reporting instructions](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately).

If the form is unavailable, check the [maintainer's profile](https://github.com/rishabh-ghosh24) for a public contact method. If none is listed, open an issue asking for a private security contact without publishing vulnerability details, credentials or customer data.

## Deployment controls

See [Destructive Operation Safety](README.md#destructive-operation-safety) for read-only mode, confirmation, history, audit retention and current limitations. For trusted shared-VM setups, see the [team deployment guide](docs/team-deployment.md).

The confirmation secret helps prevent accidental actions by a well-behaved client. It is not an independent approval channel against a compromised client. OCI IAM and the host's access controls determine the permissions available to the server process.
