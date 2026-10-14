# Deploy the read-only HTTPS MCP server

The existing `oci-logan-mcp` command remains the full stdio server used locally
or through SSH. Installing the `https` extra adds `oci-logan-mcp-http`, a
Streamable HTTP endpoint intended for remote clients such as Oracle Fusion AI
Agent Studio.

The HTTPS entry point exposes exactly six tools:

| Tool | Purpose |
| --- | --- |
| `test_connection` | Run a five-minute count query. |
| `get_log_summary` | Count records by Log Analytics source. |
| `list_log_sources` | Return source metadata in the fixed scope. |
| `list_fields` | Return namespace-level field metadata. |
| `validate_query` | Perform advisory syntax and field validation. |
| `run_query` | Run one bounded, read-only Log Analytics query. |

Every request must carry one bearer token. The server fixes the OCI compartment
or tenancy OCID at startup, disables subcompartments, limits query windows and
rows, rejects caller-supplied scope controls, rejects cluster queries, issues
one OCI query request without SDK retries, and never exposes the stdio server's
resources or mutating tools. Caddy terminates trusted TLS and proxies only to a
loopback listener.

This bearer-token implementation is appropriate for a controlled internal
endpoint or proof of concept. It is not OAuth. If your production standard
requires short-lived identity tokens, per-user authorization, revocation, or
central audit, place the service behind an approved OAuth/JWT API gateway and
keep the same fixed-scope application controls.

## 1. Prepare OCI identity and Log Analytics

These steps assume an OCI Compute VM and instance-principal authentication. Run
the service as the same OS account that owns the existing Logan configuration;
the supplied service template uses `opc`.

1. Add the VM to a dynamic group, for example with the matching rule:

   ```text
   ALL {instance.id = '<instance_ocid>'}
   ```

2. Grant only the permissions required for query, source, and field discovery.
   The following granular example uses the exact permissions listed for the OCI
   API operations:

   ```text
   Allow dynamic-group <dynamic_group_name> to {LOG_ANALYTICS_LIFECYCLE_READ, LOG_ANALYTICS_QUERY_VIEW, LOG_ANALYTICS_SOURCE_INSPECT, LOG_ANALYTICS_FIELD_INSPECT} in tenancy
   Allow dynamic-group <dynamic_group_name> to {LOGANALYTICS_LOG_GROUP_READ_LOGS, LOG_ANALYTICS_QUERYJOB_WORK_REQUEST_READ} in compartment id <compartment_ocid>
   Allow dynamic-group <dynamic_group_name> to read compartments in tenancy
   ```

   If the fixed scope is the tenancy root, change the two compartment-scoped
   statements to `in tenancy`. The [Log Analytics policy
   reference](https://docs.oracle.com/en-us/iaas/Content/Identity/policyreference/loganalyticspolicyreference.htm)
   is authoritative for the permissions required by `Query`, `ListSources`,
   and `ListFields`. Using the exact permission set also avoids unrelated
   permissions bundled into broader verbs. For example, Oracle currently maps
   `read loganalytics-queryjob-work-request` to both read and delete/cancel
   permissions. Validate the policy in your tenancy and expand it only when an
   observed API denial identifies a documented requirement.

3. Confirm that Log Analytics is already onboarded in the tenancy and that the
   target compartment contains the log groups the endpoint should query.

## 2. Install Logan and configure instance principal

Run as `opc` on the VM:

```sh
cd /home/opc
git clone https://github.com/jujufugh/logan-mcp-server.git
cd logan-mcp-server
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -e '.[https]'
export OCI_LA_AUTH_TYPE=instance_principal
venv/bin/oci-logan-mcp --setup
```

In the setup wizard, enter the Log Analytics namespace, region, and the same
compartment OCID that will be fixed in the HTTPS environment file. Test the
existing stdio setup before adding HTTP:

```sh
OCI_LA_AUTH_TYPE=instance_principal \
  venv/bin/oci-logan-mcp --user https.client --read-only
```

An MCP client should be able to initialize the process and call
`test_connection`. Stop the client after that check; the command is a long-lived
stdio server.

## 3. Create the bearer credential

Generate the token directly into a private file. The script refuses relative
paths and refuses to replace an existing credential.

```sh
cd /home/opc/logan-mcp-server
venv/bin/python scripts/generate_http_token.py \
  /home/opc/.oci-logan-mcp/http/bearer-token
stat -c '%U %G %a %n' /home/opc/.oci-logan-mcp/http/bearer-token
```

The expected owner is `opc`, and the expected mode is `600`. Never put the
token value in source control, an environment variable, a command argument,
chat, ticket, or service log. Transfer it only through your approved secret
channel to the Fusion administrator who creates the MCP tool.

## 4. Install and start the loopback service

Copy the templates and edit only the installed environment file:

```sh
sudo install -d -m 0750 -o root -g opc /etc/oci-logan-mcp-http
sudo install -m 0640 -o root -g opc \
  deploy/https/service.env.example /etc/oci-logan-mcp-http/service.env
sudo install -m 0644 deploy/https/oci-logan-mcp-http.service \
  /etc/systemd/system/oci-logan-mcp-http.service
sudoedit /etc/oci-logan-mcp-http/service.env
sudo systemctl daemon-reload
sudo systemctl enable --now oci-logan-mcp-http.service
sudo systemctl status oci-logan-mcp-http.service --no-pager
```

Set these three required values in `service.env`:

- `LOGAN_HTTP_PUBLIC_URL=https://<dns-name>/mcp`
- `LOGAN_HTTP_TOKEN_FILE=<absolute-mode-0600-token-path>`
- `LOGAN_HTTP_COMPARTMENT_ID=<fixed-compartment-or-tenancy-ocid>`

The service binds to `127.0.0.1:8765`. A direct unauthenticated check should
return `401`; that proves the process is listening without exposing the token:

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H 'Host: logan-mcp.example.com' http://127.0.0.1:8765/mcp
```

Replace the example Host with the hostname in `LOGAN_HTTP_PUBLIC_URL`.

## 5. Publish trusted HTTPS with Caddy

Create a public DNS A or AAAA record for the MCP hostname. Allow inbound TCP
443 in the VM firewall and its OCI NSG or security list. The provided Caddyfile
uses TLS-ALPN certificate validation on port 443 and disables the HTTP challenge
and HTTP redirect listener. If your organization uses a different ingress,
preserve the original Host header and proxy only to `127.0.0.1:8765`.

Install Caddy from its [official installation
instructions](https://caddyserver.com/docs/install), then:

```sh
sudo install -d -m 0755 -o root -g root /etc/caddy
sudo install -m 0644 deploy/https/Caddyfile.example /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy.service
sudo systemctl status caddy.service --no-pager
```

Replace `logan-mcp.example.com` in the Caddyfile. Caddy's [automatic HTTPS
documentation](https://caddyserver.com/docs/automatic-https) explains the DNS
and port requirements and certificate renewal behavior.

## 6. Verify from outside the VM

Run the credential-safe verifier from a trusted workstation or the VM after
public DNS resolves. It prints no token, query text, log row, compartment OCID,
or aggregate value.

```sh
venv/bin/python scripts/verify_http_endpoint.py \
  --url https://logan-mcp.example.com/mcp \
  --token-file /absolute/private/path/to/bearer-token
```

The report must pass all of these checks:

1. trusted TLS and hostname validation;
2. HTTP `401` for missing and wrong credentials;
3. MCP initialization;
4. discovery of exactly six read-only tools;
5. rejection of unknown and mutating tool names; and
6. one complete five-minute aggregate query in the server-configured scope.

SSH/stdio success alone does not prove that a SaaS client can reach this
endpoint. Do not declare the integration ready until the external verifier and
the target client's discovery and bounded tool-call checks all pass.

## 7. Add the endpoint to Fusion AI Agent Studio

Oracle's current workflow is documented in [Add MCP
Tool](https://docs.oracle.com/en/cloud/saas/fusion-ai/26c/aiaas/add-mcp-tool.html).
Labels can vary by Fusion release.

1. Open **AI Agent Studio**, then **Tools**, and create a tool of type **MCP**.
2. Enter a name, code, description, family, and product appropriate to the
   operational use case.
3. Set **Transport Type** to **StreamableHTTP**.
4. Set **Instance URL** to `https://logan-mcp.example.com/mcp`.
5. Select **API Key** authentication and enter the raw bearer token value. Do
   not add the word `Bearer`; Fusion supplies the authorization scheme.
6. Run discovery. Confirm that the six tools in the table at the top of this
   guide are the only tools returned, then create the tool.
7. Attach the tool to a test agent. Ask the agent to call `test_connection`,
   then run a known, low-volume query with a short lookback and small
   `max_results`.
8. Test a forbidden tool name and a scope override such as
   `include_subcompartments=true`; both must be rejected.

Tool discovery proves metadata access. A complete `test_connection` proves one
query at that moment. Neither proves that all expected sources are ingesting or
that ESS job health fields are populated. Validate known request IDs, source
coverage, timestamps, partial-result flags, and semantic interpretation before
an agent recommends an operational action.

## Operations and rotation

Inspect service logs without increasing application verbosity:

```sh
sudo journalctl -u oci-logan-mcp-http.service --since '30 minutes ago' --no-pager
sudo journalctl -u caddy.service --since '30 minutes ago' --no-pager
```

The HTTP process logs tool name and outcome only. It disables upstream query
logging for this process and does not log bearer tokens, query text, or rows.

To rotate the credential, create a different file, update Fusion and the
service during an agreed cutover, point `LOGAN_HTTP_TOKEN_FILE` to the new file,
restart `oci-logan-mcp-http.service`, rerun the verifier, and securely remove
the old token after confirming there are no remaining consumers. Restarting the
service also resets its in-process query-attempt counter.

For rollback, detach the MCP tool from dependent agents or workflows, disable
the Caddy and HTTP services, revoke the bearer file, and remove the inbound 443
rule only after confirming no other service uses it. The original SSH/stdio
configuration and `oci-logan-mcp` command are independent and remain available.
