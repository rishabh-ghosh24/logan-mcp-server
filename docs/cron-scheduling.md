# Scheduling Promotion

The promotion pipeline (moving high-quality personal queries to the shared
catalog) runs on-demand via:

```bash
python -m oci_logan_mcp --promote-and-exit
```

This is a short-lived process — it scans user data, promotes qualifying
queries, writes status back, and exits. Run it on a schedule to keep the
shared catalog fresh.

## Prerequisites

- Read access to `users/` directory
- Write access to `shared/` directory and each `users/*/learned_queries.yaml`
  (for status write-back)
- The same Python environment / package installation as the MCP server

## System cron (Linux/macOS)

Add to user crontab (`crontab -e`):

```
# Run promotion daily at 3 AM
0 3 * * * cd /path/to/logan-mcp-server && /path/to/python -m oci_logan_mcp --promote-and-exit >> /var/log/logan-promote.log 2>&1
```

## systemd timer (Linux)

Create `/etc/systemd/system/logan-promote.service`:

```ini
[Unit]
Description=Logan MCP learned-query promotion

[Service]
Type=oneshot
User=logan
WorkingDirectory=/path/to/logan-mcp-server
ExecStart=/path/to/python -m oci_logan_mcp --promote-and-exit
StandardOutput=journal
StandardError=journal
```

Create `/etc/systemd/system/logan-promote.timer`:

```ini
[Unit]
Description=Run logan-promote daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with:

```bash
sudo systemctl enable --now logan-promote.timer
```

## macOS launchd

Create `~/Library/LaunchAgents/com.logan-mcp.promote.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.logan-mcp.promote</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python</string>
        <string>-m</string>
        <string>oci_logan_mcp</string>
        <string>--promote-and-exit</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/logan-mcp-server</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>3</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
</dict>
</plist>
```

Load with `launchctl load ~/Library/LaunchAgents/com.logan-mcp.promote.plist`.

## GitHub Actions (for CI-deployed servers)

```yaml
name: Promote learned queries
on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

jobs:
  promote:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e .
      - run: python -m oci_logan_mcp --promote-and-exit
        env:
          LOGAN_BASE_DIR: /path/from/secrets
```

## Concurrency

`--promote-and-exit` uses a file lock (`shared/catalog.lock`) to serialize
shared-file writes and a per-user lock (`queries.lock`) for status
write-back. Running the promotion job more frequently than necessary wastes
cycles but will not corrupt data.

Do NOT run two promotion jobs simultaneously pointed at the same base_dir —
while flock will serialize them, in-process threading locks are per-invocation
and don't protect against this scenario.

## What gets promoted

See the main README for promotion thresholds (single-user vs. multi-user
interest scores, success rates). Promotion updates `promotion_status` fields
on every scanned personal entry, so operators can inspect
`users/*/learned_queries.yaml` to see why a query was/wasn't promoted.
