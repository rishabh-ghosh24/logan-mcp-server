# Windows Setup Guide

## The Problem

Windows OpenSSH (`ssh.exe`) does not properly handle stdin/stdout piping when spawned as a subprocess by MCP clients (Claude Desktop, Codex, etc.). This causes the MCP server to disconnect immediately after connecting — you'll see "Server transport closed unexpectedly" in the logs, even though the remote server is working fine.

**The fix:** Use PuTTY's `plink.exe` instead of `ssh.exe`.

## Setup

### Prerequisites

- [PuTTY](https://www.putty.org) installed on your Windows machine
- Your SSH private key file (e.g., `your-key.key`)

### Step 1: Convert Your SSH Key to PuTTY Format (.ppk)

PuTTY uses its own key format:

1. Open **PuTTYgen** (installed with PuTTY)
2. Click **Load** and select your private key file
3. Click **Save private key** and save as `.ppk` in the same directory

### Step 2: Create a PuTTY Saved Session

1. Open **PuTTY**
2. Configure:
   - **Session:** Host Name = your VM IP, Port = `22`
   - **Connection:** Seconds between keepalives = `30`
   - **Connection > Data:** Auto-login username = `opc`
   - **Connection > SSH > Auth > Credentials:** Browse to your `.ppk` key file
3. Back in **Session**, type a name (e.g., `logan-mcp`) in "Saved Sessions" and click **Save**

### Step 3: Cache the Host Key

Open Command Prompt and run:

```
"C:\Program Files\PuTTY\plink.exe" -load logan-mcp "echo hello"
```

Type `y` when prompted to accept the host key. You should see `hello` printed back. This only needs to be done once per server.

### Step 4: Configure Your MCP Client

**Claude Desktop** (`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oci-log-analytics": {
      "command": "C:\\Program Files\\PuTTY\\plink.exe",
      "args": [
        "-load", "logan-mcp",
        "-batch",
        "-T",
        "cd /home/opc/logan-mcp-server && source venv/bin/activate && oci-logan-mcp --user firstname.lastname"
      ]
    }
  }
}
```

Replace `logan-mcp` with whatever name you used for the saved session.

### Step 5: Restart Your MCP Client

Close and reopen Claude Desktop (or Codex). The server should now connect and stay connected.

## Why This Works

- **`plink.exe`** correctly handles stdin/stdout piping for MCP's stdio transport. Windows OpenSSH does not.
- **`-batch`** suppresses interactive prompts so the connection doesn't hang.
- **`-T`** disables pseudo-terminal allocation, preventing TTY escape sequences from polluting the JSON-RPC stream.
- **Keepalive (30s)** prevents the SSH connection from being dropped during idle periods.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "Cannot confirm a host key in batch mode" | Host key not cached for the saved session | Run Step 3 again: `plink -load logan-mcp "echo hello"` and type `y` |
| "Cannot answer interactive prompts in batch mode" | Missing username or key in saved session | Redo Step 2 — ensure Auto-login username and key path are set and saved |
| "Unable to use key file" | Key not in PPK format | Redo Step 1 with PuTTYgen |
| Asks for "login as:" | Auto-login username not set in saved session | Go to PuTTY > Connection > Data > set `opc` > re-save the session |
| "Software caused connection abort" after idle | Keepalive not configured | Go to PuTTY > Connection > set keepalive to 30 > re-save the session |
| Server connects then immediately disconnects | Wrong remote path or venv issue | SSH in manually and verify: `cd /home/opc/logan-mcp-server && source venv/bin/activate && oci-logan-mcp` |
| "Network error" or timeout | Firewall/security list blocking SSH | Ensure port 22 is open to your IP in the OCI security list |
| `plink: unknown option "-keepalive"` | Keepalive cannot be set via command line | Set it in the PuTTY saved session GUI instead (Step 2) |

## SSH Key Permissions (for manual testing)

If you test with `ssh.exe` directly and it refuses your key, fix permissions in PowerShell:

```powershell
$keyPath = "C:\path\to\your\key.key"
icacls $keyPath /inheritance:r
icacls $keyPath /grant:r "${env:USERNAME}:(R)"
```

This is **not needed** for the plink setup — PuTTY does not enforce file permission checks.
