# Team deployment

Use this guide for a trusted team running the stdio server on an OCI VM and connecting through SSH. For a local installation, start with the [Quick Start](../README.md#quick-start).

## Remote VM via SSH

First connect interactively and verify the host fingerprint with your administrator:

```bash
ssh -i /absolute/path/to/your-key opc@your-vm-host
```

After the verified host key is saved, the MCP client can connect without an interactive host-key prompt. Replace the host, key path, repository path and username in this local client configuration:

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "ssh",
      "args": [
        "-i", "/absolute/path/to/your-key",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ServerAliveInterval=60",
        "-o", "ServerAliveCountMax=3",
        "opc@your-vm-host",
        "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"
      ]
    }
  }
}
```

For Codex CLI, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.logan-mcp]
command = "ssh"
args = ["-i", "/absolute/path/to/your-key", "-o", "StrictHostKeyChecking=yes", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=3", "opc@your-vm-host", "cd /path/to/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"]
```

The keepalive options send a packet every 60 seconds and disconnect after three missed replies. They help detect idle SSH connections dropped by a firewall or NAT gateway. Each connection starts a server process speaking MCP over stdio.

The Windows one-click installer is distributed internally. Use the public manual connection examples for this repository.

## User identity

Use a distinct `firstname.lastname` name for each teammate. Resolution order is:

1. `--user <name>`
2. `LOGAN_USER`
3. The system `USER` value

For example:

```bash
oci-logan-mcp --user david.smith
```

Separate names select separate query, preference, history and confirmation-secret files under `~/.oci-logan-mcp/users/<username>/`. A remembered default compartment belongs to that named user.

The name is a storage selector, not authenticated identity. Connections using the same Unix account can access that account's files, and OCI operations use the process's configured OCI principal. Use separate OS accounts where filesystem isolation is required. This deployment is for trusted teammates; it does not provide per-caller OCI authorization or a public HTTPS endpoint.

## Deploying on an OCI VM

### Fresh Oracle Linux 9 VM

The optional [image setup script](../scripts/oci-initial-setup.sh) updates system packages and installs Python 3.11, OCI CLI, Docker and Java. It prepares the VM; it does not install this MCP package. Review the script and choose it only when those VM changes are wanted:

```bash
curl -fsSL https://raw.githubusercontent.com/rishabh-ghosh24/logan-mcp-server/main/scripts/oci-initial-setup.sh -o oci-initial-setup.sh
bash oci-initial-setup.sh
```

Follow its restart and login instructions. Then install the MCP package:

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
python3.11 -m venv venv
source venv/bin/activate
pip install -e .
```

### Existing VM with Python 3.11

The [existing-VM helper](../scripts/setup_oel9.sh) creates the virtual environment and installs the package. Confirm Python 3.11 is available before using it, so its legacy interpreter fallback cannot select an unsupported version:

```bash
git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git
cd logan-mcp-server
python3.11 --version && ./scripts/setup_oel9.sh
```

For Python 3.12 to 3.14, use the manual Quick Start with your selected interpreter.

### Instance principal setup

Copy the VM's instance OCID from its OCI Compute instance page. Create a dynamic group, such as `logan-mcp-dg`, matching that instance:

```text
ANY {instance.id = '<your-compute-instance-OCID>'}
```

Adapt the [IAM access examples](../README.md#iam-access) to the features and compartments the team needs. The existing broad dynamic-group example is:

```text
Allow dynamic-group logan-mcp-dg to use loganalytics-features-family in tenancy
Allow dynamic-group logan-mcp-dg to use loganalytics-resources-family in tenancy
Allow dynamic-group logan-mcp-dg to manage management-dashboard-family in tenancy
Allow dynamic-group logan-mcp-dg to read compartments in tenancy
Allow dynamic-group logan-mcp-dg to manage alarms in tenancy
Allow dynamic-group logan-mcp-dg to read metrics in tenancy
Allow dynamic-group logan-mcp-dg to manage ons-topics in tenancy
Allow dynamic-group logan-mcp-dg to use streams in tenancy
```

For a new tenancy, an administrator may first need to enable Log Analytics and its service policy:

```text
Allow service loganalytics to read loganalytics-features-family in tenancy
```

See Oracle's [Log Analytics access guide](https://docs.oracle.com/en-us/iaas/log-analytics/doc/enable-access-logging-analytics-its-resources.html) for service setup and scoped policy examples.

Configure authentication and run setup on the VM:

```bash
export OCI_LA_AUTH_TYPE=instance_principal
oci-logan-mcp --setup
```

The first server start can build Matplotlib's font cache before answering MCP initialization. Inspect stderr if startup takes longer than expected. SSH keepalives do not change an MCP client's initialization deadline.

## Audit and history retention

For a shared deployment, configure:

```yaml
audit:
  store_arguments: false
```

The equivalent environment setting is `OCI_LOGAN_AUDIT_STORE_ARGUMENTS=0`. Audit records then fingerprint query/free-text arguments and errors, retain selected numeric/boolean result summaries, and carry `text_retained: false`. Playbooks retain omission placeholders and warn that argument text was not retained; transcripts preserve the flag.

Each user's query history remains a separate file at `users/<username>/history/queries.log`, with mode `0600` and rotation. To disable history files, also set:

```yaml
logging:
  query_logging: false
```

The recent-query resource remains in memory. Operational stderr and debug logs are separate from audit retention and can include service diagnostics.

## Promoting shared templates

Promotion is a single-writer administrator task. User sessions save personal queries; an administrator chooses when to update the shared catalog:

```bash
oci-logan-mcp --promote-and-exit
oci-logan-mcp --promote-and-exit --base-dir /home/opc/.oci-logan-mcp
```

| Scenario | Minimum interest score | Minimum recorded success rate |
|---|---|---|
| One user | 4 | 0.80 |
| Matching query from at least two users | 3 | 0.70 |

Queries with no recorded executions remain pending. The shared catalog holds at most 100 entries; qualifying entries outside the final catalog are marked `pending: over_cap`. Shared examples in `get_query_examples` show their user and success counts.

Promotion sanitizes query text. Review names, descriptions and tags before sharing them across users; further shared-catalog privacy fixes remain in the publication plan. Recorded success means the execution succeeded, not that the query's interpretation has been independently checked.

Use the [scheduling guide](cron-scheduling.md) for cron, systemd and launchd examples. Scheduling is an administrator choice; installing the package does not start a promotion job.
